"""OpenAI 用 Fetcher — Pattern H、author hardcode、tags 抽出。

Phase 3 PR 3-d-2。OpenAI 公式 news feed (`https://openai.com/news/rss.xml`)
は ``<description>`` に短い概要 (~150 chars) のみで本文は HTML 詳細ページに
委譲する Pattern H ソース。

per-source 設計 (実 RSS 観察ベース、SSoT: tier1-fetcher-research.md §PR 3-d-2):

- feed が **RSS 2.0** (UTF-8、Cloudflare Pages 配信)
- ``<item>`` は ``<title>`` / ``<link>`` (絶対 URL ``/index/<slug>``) /
  ``<guid>`` (link と同値) / ``<pubDate>`` (RFC 822 GMT) /
  ``<description>`` (短い概要) / ``<category>`` (e.g., "Product",
  "Research")
- ``<author>`` / ``<dc:creator>`` / ``<media:*>`` / ``<language>`` は **未提供**
- 組織発信のため ``metadata.author = "OpenAI"`` hardcode
- ``<category>`` を ``metadata.tags`` に詰める
- attribution は news_sources 行の ``attribution_label`` ("OpenAI")
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
_DEFAULT_LANGUAGE = "en"
_AUTHOR_HARDCODED = "OpenAI"


def _strip_html(s: str) -> str:
    """HTML タグ剥がし + 空白正規化 (title 用)。"""
    if not s:
        return ""
    return _WHITESPACE_RE.sub(" ", html.unescape(_HTML_TAG_RE.sub(" ", s))).strip()


def _parse_published_at(entry: dict[str, Any]) -> PublishedAt | None:
    """feedparser の ``*_parsed`` (struct_time) を UTC ``PublishedAt`` に変換する。"""
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    if parsed is None:
        return None
    try:
        dt = datetime(*parsed[:6], tzinfo=UTC)
    except (TypeError, ValueError):
        return None
    return PublishedAt(value=dt)


def _extract_guid(entry: dict[str, Any]) -> str | None:
    raw = entry.get("id") or entry.get("guid")
    if isinstance(raw, str) and raw:
        return raw[:2048]
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


def _normalize_language(raw: str | None) -> str:
    value = (raw or _DEFAULT_LANGUAGE).replace("_", "-")
    return value[:20]


class OpenAIFetcher:
    """OpenAI 用 Pattern H Fetcher (organizational author hardcode)。

    PROVIDES に列挙したフィールドは feed-level / RSS 仕様で 100% 提供される
    前提:

    - ``language``: feed-level ``<language>`` 欠落のため hardcode "en"
    - ``guid``: ``<guid>`` (link と同値、安定 ID)
    - ``site_name``: hardcode "OpenAI"
    - ``author``: hardcode "OpenAI" (組織発信、個人 author 不公開)

    ``tags`` は probabilistic (大半の entry で 1 つ埋まるが保証しない)。
    """

    NAME: ClassVar[str] = "OpenAI"
    ENDPOINT_URL: ClassVar[str] = "https://openai.com/news/rss.xml"
    PROVIDES: ClassVar[frozenset[str]] = frozenset(
        {"language", "guid", "site_name", "author"}
    )

    async def fetch(self, source_id: int) -> AsyncIterator[FetchOutcome]:
        feed_bytes = await self._fetch_feed()
        feed = await asyncio.to_thread(feedparser.parse, feed_bytes)
        if feed.bozo and not feed.entries:
            logger.warning(
                "openai_feed_parse_error",
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
            "author": _AUTHOR_HARDCODED,
            "language": feed_language,
            "site_name": self.NAME,
        }
        if tags := _extract_tags(entry):
            metadata["tags"] = list(tags)
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
