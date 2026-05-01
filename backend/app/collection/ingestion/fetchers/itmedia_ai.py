"""ITmedia AI+ 用 Fetcher — Pattern H (RSS で URL 列挙、本文は HTML 必須)。

collection-acquisition-redesign Phase 1c-C。RSS の ``<description>`` は
~100 chars のリード文のみ (本文欠落) のため、Fetcher は本文を取りに行かず
``PendingHtmlFetch`` を yield する。後段の ``extract_html_body`` task が
``ArticleHtmlExtractor`` (trafilatura) で本文を抽出する 2 段構成。

per-source 設計:

- body は **読まない** (Pattern H、Stage 2 = HTML 抽出の責務)
- title から ``[ITmedia PC USER]`` 等の角括弧つき接頭辞を **per-source regex**
  で除去する (旧 ``fetchers/rss/itmedia.py`` の挙動を継承)
- author / tags / image_url / guid は RSS が提供しないため ``None`` / ``()`` 直書き
- language は ``feed.feed.language`` (= "ja")
- ``<guid>`` を提供しないため PROVIDES から ``guid`` を除外

旧 ``fetchers/rss/itmedia.py`` (BaseRssFetcher 継承) は本 PR で削除し、
新 Protocol に置き換える。
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
    FetchedMetadata,
    FetchOutcome,
    PendingHtmlFetch,
)
from app.models.news_source import NewsSource
from app.shared.security.safe_http import make_safe_async_client
from app.shared.security.ssrf_guard import HostBlockedError, HostResolutionError
from app.shared.value_objects.safe_url import SafeUrl

logger = structlog.get_logger(__name__)

_USER_AGENT = "Mozilla/5.0 (compatible; Vector/1.0; +https://github.com/yook11/Vector)"
_HTTP_TIMEOUT = httpx.Timeout(connect=5.0, read=30.0, write=10.0, pool=5.0)
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"\s+")
_SITE_NAME = "ITmedia AI+"
_DEFAULT_LANGUAGE = "ja"

# ITmedia AI+ のタイトル接頭辞 ([ITmedia PC USER] 等) を除去する。
# \w+ ではなく [^\]]+ を使うことで空白・マルチバイト文字を含む
# セクション名 (例: [ITmedia エンタープライズ]) にも対応する。
_TITLE_PREFIX_RE = re.compile(r"^\[[^\]]+\]\s*")


def _strip_html(s: str) -> str:
    """HTML タグを剥がして plain text に正規化する (title clean 用)。"""
    if not s:
        return ""
    return _WHITESPACE_RE.sub(" ", html.unescape(_HTML_TAG_RE.sub(" ", s))).strip()


def _strip_title_prefix(title: str) -> str:
    """ITmedia AI+ 固有: 角括弧つき接頭辞を 1 度だけ除去する。"""
    return _TITLE_PREFIX_RE.sub("", title, count=1)


def _parse_published_at(entry: dict[str, Any]) -> PublishedAt | None:
    """feedparser の ``*_parsed`` (struct_time) を UTC ``PublishedAt`` に変換する。

    Pattern H 固有: 本値が None でも Failed 降格はしない (HTML 抽出が
    ``published_at`` を出してくれれば merge 後に最終確定する)。
    """
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    if parsed is None:
        return None
    try:
        dt = datetime(*parsed[:6], tzinfo=UTC)
    except (TypeError, ValueError):
        return None
    return PublishedAt(value=dt)


def _normalize_language(raw: str | None) -> str:
    """``en_US`` / ``en-us`` / ``en-US`` の表記揺れを ``en-US`` 系に統一。"""
    value = (raw or _DEFAULT_LANGUAGE).replace("_", "-")
    return value[:20]


class ITmediaAIFetcher:
    """ITmedia AI+ 用 Pattern H Fetcher。

    PROVIDES に列挙したフィールドは feed-level で 100% 提供される前提:

    - ``language``: feed-level ``<channel><language>`` ("ja")
    - ``site_name``: hardcode "ITmedia AI+"

    ITmedia 系 RSS は ``<guid>`` / ``<author>`` / ``<category>`` /
    ``<media:content>`` を出さないため、これらは PROVIDES に含めず metadata
    でも ``None`` / ``()`` を直書きする。
    """

    PROVIDES: ClassVar[frozenset[str]] = frozenset({"language", "site_name"})

    async def fetch(self, source: NewsSource) -> AsyncIterator[FetchOutcome]:
        feed_text = await self._fetch_feed(source)
        feed = await asyncio.to_thread(feedparser.parse, feed_text)
        if feed.bozo and not feed.entries:
            logger.warning(
                "itmedia_ai_feed_parse_error",
                source=source.name,
                error=str(feed.bozo_exception),
            )
            raise PermanentFetchError(
                f"feed parse error: {source.name}: {feed.bozo_exception}"
            )

        feed_language = _normalize_language(feed.feed.get("language"))

        for entry in feed.entries:
            yield self._convert_entry(entry, source, feed_language)

    async def _fetch_feed(self, source: NewsSource) -> str:
        async with make_safe_async_client(
            headers={"User-Agent": _USER_AGENT},
            verify=True,
            timeout=_HTTP_TIMEOUT,
        ) as client:
            try:
                response = await client.get(str(source.endpoint_url))
                response.raise_for_status()
            except httpx.HTTPStatusError as e:
                status = e.response.status_code
                if status in (403, 404, 410, 451):
                    raise PermanentFetchError(f"HTTP {status}: {source.name}") from e
                raise TemporaryFetchError(f"HTTP {status}: {source.name}") from e
            except httpx.RequestError as e:
                raise TemporaryFetchError(f"request error: {source.name}: {e}") from e
            except HostBlockedError as e:
                raise PermanentFetchError(str(e)) from e
            except HostResolutionError as e:
                raise TemporaryFetchError(str(e)) from e
            return response.text

    def _convert_entry(
        self,
        entry: dict[str, Any],
        source: NewsSource,
        feed_language: str,
    ) -> FetchOutcome:
        """1 entry を ``FetchOutcome`` に変換する純関数。

        Pattern H 固有の品質ゲート:

        - ``title`` 空 → ``Failed(title_missing)``
        - ``link`` 不正 → ``Failed(extraction_empty)``
        - ``published_at`` 欠落 → **Failed しない** (HTML 補完を待つ)
        - ``body`` は本実装では検査しない (Stage 2 の責務)
        """
        title = _strip_title_prefix(_strip_html(entry.get("title", "") or ""))
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

        metadata = FetchedMetadata(
            author=None,
            tags=(),
            image_url=None,
            language=feed_language,
            guid=None,
            site_name=_SITE_NAME,
        )

        return PendingHtmlFetch(
            title=title,
            source_id=source.id,
            source_url=source_url,
            published_at_hint=published_at_hint,
            metadata=metadata,
        )
