"""NIST (米国国立標準技術研究所) 用 Fetcher — Pattern H、RSS 2.0、UTF-8。

Phase 3 PR 3-a。NIST News feed (`/news-events/news/rss.xml`) を Pattern H
で扱う。description は短い概要 (~80 chars) で本文は HTML 抽出に委譲。

per-source 設計 (実 RSS 観察ベース、SSoT: tier1-fetcher-research.md §PR 3-a):

- feed が **RSS 2.0** (UTF-8、Drupal 7 generator)
- ``<item>`` が ``<title>`` / ``<link>`` / ``<description>`` (短い概要) /
  ``<pubDate>`` RFC 822 / ``<dc:creator>`` / ``<guid isPermaLink="true">`` を提供
- per-entry の image_url / tags は **未提供** のため metadata は None / () 直書き
- ``<dc:creator>`` は recurring author (e.g., "Sarah Henderson") → metadata.author
  に詰めるが、PROVIDES には含めない (将来 multi-author 化や欠落の蓋然性に備える)
- robots.txt: `*` Allow、Crawl-delay なし、cron 側で defensive interval を確保
- License: 17 U.S.C. §105 (Public Domain)、attribution は news_sources 行の
  ``attribution_label`` カラム ("NIST") に格納
- ``feedparser.parse(response.content)`` (bytes) を採用 (UTF-8/他 encoding 両対応)
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
_DEFAULT_LANGUAGE = "en"


def _strip_html(s: str) -> str:
    """HTML タグ剥がし + 空白正規化 (title 用)。"""
    if not s:
        return ""
    return _WHITESPACE_RE.sub(" ", html.unescape(_HTML_TAG_RE.sub(" ", s))).strip()


def _parse_published_at(entry: dict[str, Any]) -> PublishedAt | None:
    """feedparser の ``*_parsed`` (struct_time) を UTC ``PublishedAt`` に変換する。

    Pattern H 固有: 本値が None でも Failed 降格はしない (HTML 抽出時に
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
    """``<guid isPermaLink="true">`` の値を返す。"""
    raw = entry.get("id") or entry.get("guid")
    if isinstance(raw, str) and raw:
        return raw[:2048]
    return None


def _extract_author(entry: dict[str, Any]) -> str | None:
    """``<dc:creator>`` を 200 chars に切り詰めて返す。"""
    raw = entry.get("author") or entry.get("dc_creator")
    if isinstance(raw, str):
        cleaned = _strip_html(raw)
        return cleaned[:200] if cleaned else None
    return None


def _normalize_language(raw: str | None) -> str:
    """``en`` / ``en-US`` の表記揺れを統一。"""
    value = (raw or _DEFAULT_LANGUAGE).replace("_", "-")
    return value[:20]


class NISTFetcher:
    """NIST (米国国立標準技術研究所) 用 Pattern H Fetcher。

    PROVIDES に列挙したフィールドは feed-level / RSS 仕様で 100% 提供される
    前提:

    - ``language``: feed-level ``<language>en</language>``
    - ``guid``: ``<guid isPermaLink="true">`` (RSS 2.0 仕様で必須相当)
    - ``site_name``: hardcode "NIST"
    """

    NAME: ClassVar[str] = "NIST"
    ENDPOINT_URL: ClassVar[str] = "https://www.nist.gov/news-events/news/rss.xml"
    PROVIDES: ClassVar[frozenset[str]] = frozenset({"language", "guid", "site_name"})

    async def fetch(self, source_id: int) -> AsyncIterator[FetchOutcome]:
        feed_bytes = await self._fetch_feed()
        feed = await asyncio.to_thread(feedparser.parse, feed_bytes)
        if feed.bozo and not feed.entries:
            logger.warning(
                "nist_feed_parse_error",
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

        - ``title`` 空 → ``Failed(title_missing)``
        - ``link`` 不正 → ``Failed(extraction_empty)``
        - ``published_at`` 欠落 → **Failed しない** (HTML 補完を待つ)
        - ``body`` は本実装では検査しない (Stage 2 の責務)
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

        metadata: dict[str, Any] = {
            "language": feed_language,
            "site_name": self.NAME,
        }
        if author := _extract_author(entry):
            metadata["author"] = author
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
