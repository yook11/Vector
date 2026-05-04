"""TechCrunch 用 Fetcher — Pattern H (RSS で URL 列挙、本文は HTML 必須) の参照実装。

collection-acquisition-redesign Phase 1b'。新 ``Fetcher`` Protocol を満たし、
1 entry ずつ ``PendingHtmlFetch`` (or ``Failed``) を yield する。

TC の RSS feed は ``<description>`` にリード文 (~140 chars) しか含まず、
``<content:encoded>`` も提供しない (`spec collection-source-rss-research.md`)。
このため Fetcher は **本文を取りに行かない** — URL + title + RSS metadata
を ``PendingHtmlFetch`` として yield し、後段の ``extract_html_body`` task
が ``ArticleHtmlExtractor`` (trafilatura) で本文を抽出する 2 段構成。

リファクタの本質的目的: 旧パイプラインは RSS が出している author / tags /
image_url / language / guid を全部捨てていた。本実装は ``FetchedEntry.metadata``
dict で capture-everything を担い、Stage 1 の ``pipeline_events.payload`` に焼く。

per-source 独立実装 (Pattern H 共通基底は作らない): 「source ごとに取れる
ものが違う」が新 Protocol の設計動機。VB Fetcher と構造は似るが共通基底化
すると差異の表現が逆に難しくなるため、code copy で許容する。

旧 ``fetchers/rss/techcrunch.py`` (BaseRssFetcher 継承の薄いスタブ) は
本 PR で削除し、新 Protocol に置き換える。
"""

from __future__ import annotations

import asyncio
import html
import re
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any, ClassVar

import feedparser
import httpx
import structlog

from app.collection.errors import PermanentFetchError, TemporaryFetchError
from app.collection.extraction.domain.value_objects import PublishedAt
from app.collection.ingestion.domain.fetched_article import (
    Failed,
    FailureReason,
    FetchedEntry,
    FetchOutcome,
    PendingHtmlFetch,
)
from app.shared.security.safe_http import make_safe_async_client
from app.shared.security.ssrf_guard import HostBlockedError, HostResolutionError
from app.shared.value_objects.safe_url import SafeUrl

logger = structlog.get_logger(__name__)

_USER_AGENT = "Mozilla/5.0 (compatible; Vector/1.0; +https://github.com/yook11/Vector)"
_HTTP_TIMEOUT = httpx.Timeout(connect=5.0, read=30.0, write=10.0, pool=5.0)
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"\s+")
_DEFAULT_LANGUAGE = "en-US"


def _strip_html(s: str) -> str:
    """HTML タグを剥がして plain text に正規化する (title clean 用)。

    TC RSS の ``<title>`` は通常タグを含まないが、稀に ``<![CDATA[...]]>``
    に HTML entity (e.g. ``&amp;``) を含むので decode する。本文は HTML
    取得後に trafilatura が処理するため、本関数は title 専用。
    """
    if not s:
        return ""
    return _WHITESPACE_RE.sub(" ", html.unescape(_HTML_TAG_RE.sub(" ", s))).strip()


def _parse_published_at(entry: dict[str, Any]) -> PublishedAt | None:
    """feedparser の ``*_parsed`` (struct_time) を UTC ``PublishedAt`` に変換する。

    Pattern H 固有: 本値が None でも Failed 降格はしない (HTML 抽出が
    ``published_at`` を出してくれれば merge 後に最終確定する)。``PendingHtmlFetch``
    の ``published_at_hint`` に格納される。
    """
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    if parsed is None:
        return None
    try:
        dt = datetime(*parsed[:6], tzinfo=UTC)
    except (TypeError, ValueError):
        return None
    return PublishedAt(value=dt)


def _extract_image_url(entry: dict[str, Any]) -> SafeUrl | None:
    """``<media:content>`` / ``<media:thumbnail>`` から画像 URL を取り出す。

    TC は記事ごとに ``<media:content>`` を出すことが多いが、必ずではない
    (probabilistic、PROVIDES に含めない)。
    """
    for key in ("media_content", "media_thumbnail"):
        media = entry.get(key)
        if not isinstance(media, list) or not media:
            continue
        first = media[0]
        if not isinstance(first, dict):
            continue
        url = first.get("url")
        if not isinstance(url, str) or not url:
            continue
        try:
            return SafeUrl(url)
        except ValueError:
            continue
    return None


def _extract_tags(entry: dict[str, Any]) -> tuple[str, ...]:
    """feedparser の ``tags`` (= ``<category>``) を tuple 化する。"""
    tags = entry.get("tags")
    if not isinstance(tags, list):
        return ()
    return tuple(
        t["term"]
        for t in tags
        if isinstance(t, dict) and isinstance(t.get("term"), str) and t["term"]
    )


def _extract_guid(entry: dict[str, Any]) -> str | None:
    """``<guid>`` (feedparser では ``id`` にマップ) を取り出す。

    TC は WordPress 製で ``?p=<post_id>`` 形式の永続 ID を出す。RSS 仕様で
    必須項目なので PROVIDES に含める。
    """
    raw = entry.get("id") or entry.get("guid")
    if isinstance(raw, str) and raw:
        return raw[:2048]
    return None


def _normalize_language(raw: str | None) -> str:
    """``en_US`` / ``en-us`` / ``en-US`` の表記揺れを ``en-US`` 系に統一。"""
    value = (raw or _DEFAULT_LANGUAGE).replace("_", "-")
    return value[:20]


class TechCrunchFetcher:
    """TechCrunch 用 Pattern H Fetcher。

    PROVIDES に列挙したフィールドは feed-level / RSS 仕様で 100% 提供される
    前提:

    - ``language``: feed-level ``<channel><language>`` (TC は固定で en-US)
    - ``guid``: RSS 仕様で entry 必須項目 (TC は ``?p=<id>`` 形式)
    - ``site_name``: hardcode "TechCrunch"

    ``author`` / ``tags`` / ``image_url`` は probabilistic なため metadata
    に詰めるが PROVIDES には含めない。
    """

    NAME: ClassVar[str] = "TechCrunch"
    ENDPOINT_URL: ClassVar[str] = "https://techcrunch.com/feed/"
    PROVIDES: ClassVar[frozenset[str]] = frozenset({"language", "guid", "site_name"})

    async def fetch(self, source_id: int) -> AsyncIterator[FetchOutcome]:
        feed_text = await self._fetch_feed()
        feed = await asyncio.to_thread(feedparser.parse, feed_text)
        if feed.bozo and not feed.entries:
            logger.warning(
                "techcrunch_feed_parse_error",
                source=self.NAME,
                error=str(feed.bozo_exception),
            )
            raise PermanentFetchError(
                f"feed parse error: {self.NAME}: {feed.bozo_exception}"
            )

        feed_language = _normalize_language(feed.feed.get("language"))

        for entry in feed.entries:
            yield self._convert_entry(entry, source_id, feed_language)

    async def _fetch_feed(self) -> str:
        """feed を取得して text を返す。HTTP 系の失敗は Permanent/Temporary に翻訳。"""
        async with make_safe_async_client(
            headers={"User-Agent": _USER_AGENT},
            verify=True,
            timeout=_HTTP_TIMEOUT,
        ) as client:
            try:
                response = await client.get(self.ENDPOINT_URL)
                response.raise_for_status()
            except httpx.HTTPStatusError as e:
                status = e.response.status_code
                if status in (403, 404, 410, 451):
                    raise PermanentFetchError(f"HTTP {status}: {self.NAME}") from e
                raise TemporaryFetchError(f"HTTP {status}: {self.NAME}") from e
            except httpx.RequestError as e:
                raise TemporaryFetchError(f"request error: {self.NAME}: {e}") from e
            except HostBlockedError as e:
                raise PermanentFetchError(str(e)) from e
            except HostResolutionError as e:
                raise TemporaryFetchError(str(e)) from e
            return response.text

    def _convert_entry(
        self,
        entry: dict[str, Any],
        source_id: int,
        feed_language: str,
    ) -> FetchOutcome:
        """1 entry を ``FetchOutcome`` に変換する純関数 (テスト容易性のため切出)。

        Pattern H 固有の品質ゲート (Pattern R より緩い):

        - ``title`` 空 → ``Failed(title_missing)``
        - ``link`` 不正 → ``Failed(extraction_empty)`` (URL invalid)
        - ``published_at`` 欠落 → **Failed しない** (HTML 補完を待つ)
        - ``body`` は本実装では検査しない (Stage 2 = ``extract_html_body`` の
          責務)
        """
        title = _strip_html(entry.get("title", "") or "")
        if not title:
            return Failed(
                reason=FailureReason(
                    code="title_missing",
                    retryable=False,
                    detail="rss_title_missing",
                )
            )
        title = title[:500]

        link = entry.get("link", "") or ""
        try:
            source_url = SafeUrl(link)
        except ValueError:
            return Failed(
                reason=FailureReason(
                    code="extraction_empty",
                    retryable=False,
                    detail=f"invalid_link:{link[:100]}",
                )
            )

        published_at_hint = _parse_published_at(entry)

        author = entry.get("author")
        if isinstance(author, str) and author:
            author = author[:200]
        else:
            author = None

        metadata: dict[str, Any] = {
            "language": feed_language,
            "site_name": self.NAME,
        }
        if author:
            metadata["author"] = author
        if tags := _extract_tags(entry):
            metadata["tags"] = list(tags)
        if image_url := _extract_image_url(entry):
            metadata["image_url"] = str(image_url)
        if guid := _extract_guid(entry):
            metadata["guid"] = guid

        return FetchedEntry(
            item=PendingHtmlFetch(
                title=title,
                source_id=source_id,
                source_url=source_url,
                published_at_hint=published_at_hint,
            ),
            metadata=metadata,
        )
