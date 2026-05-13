"""Spaceflight Now 用 Fetcher — Pattern R (RSS-only)。

collection-acquisition-redesign Phase 1c-A1。新聞社型 CMS、boilerplate なし。
launch update 系で本文 1200 chars 程度の短記事が常態 (1200-6300 chars)。

per-source 設計:

- body は ``entry.content[0].value`` を**直取り**。``<description>`` タグ自体
  が存在しないため summary fallback の概念がない
- author は **``None`` 直書き** — 本ソースは journalist byline を提供しない
  設計 (``<dc:creator>`` / ``<author>`` 双方なし)
- image は **``None`` 直書き** — 本ソースの RSS は ``<media:content>`` を
  提供しない (本文内 HTML img 埋め込みのみ)
- language は **hardcoded ``"en-US"``** — ``<channel>/<language>`` 提供なし

旧 ``fetchers/rss/spaceflight_now.py`` (BaseRssFetcher 継承の薄スタブ) は
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

from app.collection.article.domain.article import ReadyForArticle
from app.collection.article.domain.value_objects import PublishedAt
from app.collection.errors import PermanentFetchError, TemporaryFetchError
from app.collection.fetchers.outcome import (
    FetchedEntry,
    FetchOutcome,
    SourceFetchFailed,
    SourceFetchFailureReason,
)
from app.shared.security.safe_http import make_safe_async_client
from app.shared.security.ssrf_guard import HostBlockedError, HostResolutionError
from app.shared.value_objects.safe_url import SafeUrl

logger = structlog.get_logger(__name__)

_USER_AGENT = "Mozilla/5.0 (compatible; Vector/1.0; +https://github.com/yook11/Vector)"
_HTTP_TIMEOUT = httpx.Timeout(connect=5.0, read=30.0, write=10.0, pool=5.0)
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"\s+")
_HARDCODED_LANGUAGE = "en-US"


def _strip_html(s: str) -> str:
    """HTML タグを剥がして plain text に正規化する。"""
    if not s:
        return ""
    return _WHITESPACE_RE.sub(" ", html.unescape(_HTML_TAG_RE.sub(" ", s))).strip()


def _extract_body(entry: dict[str, Any]) -> str:
    """``<content:encoded>`` を直取り。本ソースの RSS には description が無い。"""
    contents = entry.get("content")
    if isinstance(contents, list) and contents:
        first = contents[0]
        if isinstance(first, dict):
            value = first.get("value")
            if isinstance(value, str):
                return value
    return ""


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
    """``<guid>`` (feedparser では ``id`` にマップ) を取り出す。"""
    raw = entry.get("id") or entry.get("guid")
    if isinstance(raw, str) and raw:
        return raw[:2048]
    return None


class SpaceflightNowFetcher:
    """Spaceflight Now 用 RSS-only Fetcher。

    本ソースは author / image_url / language を RSS で提供しないため、metadata
    で ``None`` 直書き / hardcode default を採用する。``feed.feed.language``
    も読まない (実 feed に存在しない)。
    """

    NAME: ClassVar[str] = "Spaceflight Now"
    ENDPOINT_URL: ClassVar[str] = "https://spaceflightnow.com/feed/"
    PROVIDES: ClassVar[frozenset[str]] = frozenset({"language", "guid", "site_name"})

    async def fetch(self, source_id: int) -> AsyncIterator[FetchOutcome]:
        feed_text = await self._fetch_feed()
        feed = await asyncio.to_thread(feedparser.parse, feed_text)
        if feed.bozo and not feed.entries:
            logger.warning(
                "spaceflight_now_feed_parse_error",
                source=self.NAME,
                error=str(feed.bozo_exception),
            )
            raise PermanentFetchError(
                f"feed parse error: {self.NAME}: {feed.bozo_exception}"
            )

        for entry in feed.entries:
            yield self._convert_entry(entry, source_id)

    async def _fetch_feed(self) -> str:
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

        body = _strip_html(_extract_body(entry))
        if len(body) < 50:
            return SourceFetchFailed(
                reason=SourceFetchFailureReason(
                    code="body_too_short",
                    retryable=False,
                    detail=f"rss_body_len={len(body)}",
                )
            )

        published_at = _parse_published_at(entry)
        if published_at is None:
            return SourceFetchFailed(
                reason=SourceFetchFailureReason(
                    code="published_at_missing",
                    retryable=False,
                    detail="rss_pubdate_missing",
                )
            )

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

        try:
            ready = ReadyForArticle(
                title=title,
                body=body,
                published_at=published_at,
                source_id=source_id,
                source_url=source_url,
            )
        except ValueError as e:
            return SourceFetchFailed(
                reason=SourceFetchFailureReason(
                    code="other",
                    retryable=False,
                    detail=f"invariant_violation:{e}",
                )
            )

        metadata: dict[str, Any] = {
            "language": _HARDCODED_LANGUAGE,
            "site_name": self.NAME,
        }
        if tags := _extract_tags(entry):
            metadata["tags"] = list(tags)
        if guid := _extract_guid(entry):
            metadata["guid"] = guid

        return FetchedEntry(item=ready, metadata=metadata)
