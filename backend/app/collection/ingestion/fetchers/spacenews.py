"""SpaceNews 用 Fetcher — Pattern R+H (Pattern H 設計で実装)。

collection-acquisition-redesign Phase 1d。SpaceNews の RSS は
``<description>`` にリード文 + 画像 + truncate signature
("appeared first on") のみで full body を出さないため、Fetcher は本文を
取りに行かず ``PendingHtmlFetch`` を yield する
(`spec collection-source-rss-research.md` の Pattern R+H 分類)。

per-source 設計 (実 RSS 観察ベース):

- body は **読まない** (Pattern H、Stage 2 = HTML 抽出の責務)
- ``<dc:creator>`` を author に採用
- ``<category>`` 多数を tags に採用
- ``<media:>`` namespace 未提供 → image_url=None 直書き
- ``<guid isPermaLink="false">`` (``?p=<id>`` 形式) を採用
- language は feed-level "en-US"

CleanTechnica と構造同型。
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
_SITE_NAME = "SpaceNews"
_DEFAULT_LANGUAGE = "en-US"


def _strip_html(s: str) -> str:
    if not s:
        return ""
    return _WHITESPACE_RE.sub(" ", html.unescape(_HTML_TAG_RE.sub(" ", s))).strip()


def _parse_published_at(entry: dict[str, Any]) -> PublishedAt | None:
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    if parsed is None:
        return None
    try:
        dt = datetime(*parsed[:6], tzinfo=UTC)
    except (TypeError, ValueError):
        return None
    return PublishedAt(value=dt)


def _extract_tags(entry: dict[str, Any]) -> tuple[str, ...]:
    tags = entry.get("tags")
    if not isinstance(tags, list):
        return ()
    return tuple(
        t["term"]
        for t in tags
        if isinstance(t, dict) and isinstance(t.get("term"), str) and t["term"]
    )


def _extract_guid(entry: dict[str, Any]) -> str | None:
    raw = entry.get("id") or entry.get("guid")
    if isinstance(raw, str) and raw:
        return raw[:2048]
    return None


def _normalize_language(raw: str | None) -> str:
    value = (raw or _DEFAULT_LANGUAGE).replace("_", "-")
    return value[:20]


class SpaceNewsFetcher:
    """SpaceNews 用 Pattern H Fetcher (Pattern R+H = HTML 必須)。"""

    PROVIDES: ClassVar[frozenset[str]] = frozenset({"language", "guid", "site_name"})

    async def fetch(self, source: NewsSource) -> AsyncIterator[FetchOutcome]:
        feed_text = await self._fetch_feed(source)
        feed = await asyncio.to_thread(feedparser.parse, feed_text)
        if feed.bozo and not feed.entries:
            logger.warning(
                "spacenews_feed_parse_error",
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

        raw_author = entry.get("author")
        if isinstance(raw_author, str) and raw_author:
            author = _strip_html(raw_author)[:200] or None
        else:
            author = None

        metadata = FetchedMetadata(
            author=author,
            tags=_extract_tags(entry),
            image_url=None,
            language=feed_language,
            guid=_extract_guid(entry),
            site_name=_SITE_NAME,
        )

        return PendingHtmlFetch(
            title=title,
            source_id=source.id,
            source_url=source_url,
            published_at_hint=published_at_hint,
            metadata=metadata,
        )
