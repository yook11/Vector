"""MEXT (文部科学省) 用 Fetcher — Pattern H、RDF (RSS 1.0)、UTF-8。

Phase 3 PR 3-h-1。文部科学省の新着情報 RDF feed (`/b_menu/news/index.rdf`)
を JPCERT/CC 同型の Pattern H で扱う。

per-source 設計 (実 RSS 観察ベース、SSoT: tier1-fetcher-research.md §PR 3-h-1):

- feed が **RDF (RSS 1.0)** ルート (``<rdf:RDF>``)、UTF-8 エンコード
- ``<item rdf:about="URL">`` の URL を feedparser が ``entry.id`` にマップ
- ``<description>`` は **空** であることが多く、本文は HTML から抽出
  (Pattern H、Stage 2 の責務)
- ``<dc:date>`` ISO 8601 → feedparser 標準経路で ``published_parsed`` を populate
- per-entry の author / tags / image_url は **未提供** のため metadata は
  None / () 直書き
- robots.txt は 404、自主規制 60s 以上の crawl interval を遵守 (cron 側で確保)
- License: 政府標準利用規約 2.0 + CC BY 4.0 互換、attribution は news_sources
  行の ``attribution_label`` カラムに格納
- ``feedparser.parse(response.content)`` (bytes) を採用。XML 宣言の encoding
  を feedparser に sniff させて safer (UTF-8 / Shift_JIS 両対応の統一経路)
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

from app.collection.article.domain.value_objects import PublishedAt
from app.collection.errors import PermanentFetchError, TemporaryFetchError
from app.collection.fetchers.outcome import (
    FetchedEntry,
    FetchOutcome,
    SourceFetchFailed,
    SourceFetchFailureReason,
)
from app.collection.incomplete_article.domain.incomplete_article import (
    IncompleteArticle,
)
from app.shared.security.safe_http import make_safe_async_client
from app.shared.security.ssrf_guard import HostBlockedError, HostResolutionError
from app.shared.value_objects.safe_url import SafeUrl

logger = structlog.get_logger(__name__)

_USER_AGENT = "Mozilla/5.0 (compatible; Vector/1.0; +https://github.com/yook11/Vector)"
_HTTP_TIMEOUT = httpx.Timeout(connect=5.0, read=30.0, write=10.0, pool=5.0)
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"\s+")
_DEFAULT_LANGUAGE = "ja"


def _strip_html(s: str) -> str:
    """HTML タグ剥がし + 空白正規化 (title 用)。"""
    if not s:
        return ""
    return _WHITESPACE_RE.sub(" ", html.unescape(_HTML_TAG_RE.sub(" ", s))).strip()


def _parse_published_at(entry: dict[str, Any]) -> PublishedAt | None:
    """feedparser の ``*_parsed`` (struct_time) を UTC ``PublishedAt`` に変換する。

    Pattern H 固有: 本値が None でも SourceFetchFailed 降格はしない (HTML 抽出時に
    補完される)。
    """
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    if parsed is None:
        return None
    try:
        dt = datetime(*parsed[:6], tzinfo=UTC)
    except (TypeError, ValueError):
        return None
    return PublishedAt(value=dt)


def _extract_guid(entry: dict[str, Any]) -> str | None:
    """``<item rdf:about="URL">`` を feedparser がマップした ``entry.id`` を返す。"""
    raw = entry.get("id") or entry.get("guid")
    if isinstance(raw, str) and raw:
        return raw[:2048]
    return None


def _normalize_language(raw: str | None) -> str:
    """``ja`` / ``ja-JP`` の表記揺れを統一。"""
    value = (raw or _DEFAULT_LANGUAGE).replace("_", "-")
    return value[:20]


class MEXTFetcher:
    """MEXT (文部科学省) 用 Pattern H Fetcher。

    PROVIDES に列挙したフィールドは feed-level / RDF 仕様で 100% 提供される
    前提:

    - ``language``: feed-level ``xml:lang="ja"``
    - ``guid``: ``<item rdf:about="URL">`` (RDF 必須属性)
    - ``site_name``: hardcode "MEXT"
    """

    NAME: ClassVar[str] = "MEXT"
    ENDPOINT_URL: ClassVar[str] = "https://www.mext.go.jp/b_menu/news/index.rdf"
    PROVIDES: ClassVar[frozenset[str]] = frozenset({"language", "guid", "site_name"})

    async def fetch(self, source_id: int) -> AsyncIterator[FetchOutcome]:
        feed_bytes = await self._fetch_feed()
        feed = await asyncio.to_thread(feedparser.parse, feed_bytes)
        if feed.bozo and not feed.entries:
            logger.warning(
                "mext_feed_parse_error",
                source=self.NAME,
                error=str(feed.bozo_exception),
            )
            raise PermanentFetchError(
                f"feed parse error: {self.NAME}: {feed.bozo_exception}"
            )

        feed_language = _normalize_language(feed.feed.get("language"))

        for entry in feed.entries:
            yield self._convert_entry(entry, source_id, feed_language)

    async def _fetch_feed(self) -> bytes:
        """feed を取得して **bytes** を返す (XML 宣言の encoding を feedparser に
        sniff させるため、charset 推定を httpx に任せない)。
        """
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
            return response.content

    def _convert_entry(
        self,
        entry: dict[str, Any],
        source_id: int,
        feed_language: str,
    ) -> FetchOutcome:
        """1 entry を ``FetchOutcome`` に変換する純関数。

        Pattern H 固有の品質ゲート:

        - ``title`` 空 → ``SourceFetchFailed(title_missing)``
        - ``link`` 不正 → ``SourceFetchFailed(extraction_empty)``
        - ``published_at`` 欠落 → **SourceFetchFailed しない** (HTML 補完を待つ)
        - ``body`` は本実装では検査しない (Stage 2 の責務)
        """
        title = _strip_html(entry.get("title", "") or "")
        if not title:
            return SourceFetchFailed(
                reason=SourceFetchFailureReason(
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
            return SourceFetchFailed(
                reason=SourceFetchFailureReason(
                    code="extraction_empty",
                    retryable=False,
                    detail=f"invalid_link:{link[:100]}",
                )
            )

        published_at_hint = _parse_published_at(entry)

        metadata: dict[str, Any] = {
            "language": feed_language,
            "site_name": self.NAME,
        }
        if guid := _extract_guid(entry):
            metadata["guid"] = guid

        return FetchedEntry(
            item=IncompleteArticle(
                title=title,
                source_id=source_id,
                source_url=source_url,
                published_at_hint=published_at_hint,
            ),
            metadata=metadata,
        )
