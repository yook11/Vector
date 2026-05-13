"""Cloudflare Blog 用 Fetcher — Pattern R (RSS-only)、複数 author 対応。

Phase 3 PR 3-d-1。Cloudflare 公式 blog (`https://blog.cloudflare.com/rss/`) は
``<content:encoded>`` に full body (~10000 chars) を持つ Pattern R ソース。

per-source 設計 (実 RSS 観察ベース、SSoT: tier1-fetcher-research.md §PR 3-d-1):

- feed が **RSS 2.0** (UTF-8、Cloudflare 自社フィード生成器)
- ``<item>`` は ``<title>`` (CDATA) / ``<link>`` / ``<dc:creator>`` 多重 /
  ``<content:encoded>`` (full body, CDATA) / ``<description>`` (短い概要) /
  ``<pubDate>`` GMT / ``<guid>`` (短いハッシュ ID) を提供
- ``<dc:creator>`` は **複数執筆者** が出るため ``metadata.authors`` (tuple)
  に詰め、後方互換のため先頭値を ``metadata.author`` にも duplicate する
- license: 公式に再配布可否は明示されていないが、Tier 1 認定済 (Phase 2 法務
  リサーチ)。attribution は news_sources 行の ``attribution_label``
  ("The Cloudflare Blog") に格納
- ``<media:content>`` は提供されないため image_url は None 直書き
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
    ReadyForArticle,
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
_AUTHOR_MAX_LENGTH = 200


def _strip_html(s: str) -> str:
    """HTML タグ剥がし + 空白正規化。"""
    if not s:
        return ""
    return _WHITESPACE_RE.sub(" ", html.unescape(_HTML_TAG_RE.sub(" ", s))).strip()


def _extract_body(entry: dict[str, Any]) -> str:
    """``<content:encoded>`` を直取り (Cloudflare では常に full body 込み)。"""
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


def _extract_authors(entry: dict[str, Any]) -> tuple[str, ...]:
    """``<dc:creator>`` 多重を tuple 化する。重複は除去する。

    feedparser は複数 ``<dc:creator>`` を ``entry.authors`` (= list of
    ``{"name": ...}``) に正規化する。1 名のときも list として現れる。
    """
    raw = entry.get("authors")
    if not isinstance(raw, list):
        return ()
    out: list[str] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        if not isinstance(name, str):
            continue
        cleaned = _strip_html(name)[:_AUTHOR_MAX_LENGTH]
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            out.append(cleaned)
    return tuple(out)


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
    """``<guid>`` を取り出す (Cloudflare では短いハッシュ ID)。"""
    raw = entry.get("id") or entry.get("guid")
    if isinstance(raw, str) and raw:
        return raw[:2048]
    return None


def _normalize_language(raw: str | None) -> str:
    """``en_US`` / ``en-us`` / ``en-US`` の表記揺れを ``en-US`` 系に統一。"""
    value = (raw or _DEFAULT_LANGUAGE).replace("_", "-")
    return value[:20]


class CloudflareBlogFetcher:
    """Cloudflare Blog 用 RSS-only Pattern R Fetcher。

    PROVIDES に列挙したフィールドは feed-level / RSS 仕様で 100% 提供される
    前提:

    - ``language``: feed-level ``<language>en-us</language>``
    - ``guid``: ``<guid>`` (RSS 2.0 仕様)
    - ``site_name``: hardcode "The Cloudflare Blog"

    ``authors`` / ``tags`` は probabilistic (大半の entry で埋まるが保証しない)。
    """

    NAME: ClassVar[str] = "The Cloudflare Blog"
    ENDPOINT_URL: ClassVar[str] = "https://blog.cloudflare.com/rss/"
    PROVIDES: ClassVar[frozenset[str]] = frozenset({"language", "guid", "site_name"})

    async def fetch(self, source_id: int) -> AsyncIterator[FetchOutcome]:
        feed_bytes = await self._fetch_feed()
        feed = await asyncio.to_thread(feedparser.parse, feed_bytes)
        if feed.bozo and not feed.entries:
            logger.warning(
                "cloudflare_feed_parse_error",
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
            return Failed(
                reason=FailureReason(
                    code="title_missing",
                    retryable=False,
                    detail="rss_title_missing",
                )
            )
        title = title[:500]

        body = _strip_html(_extract_body(entry))
        if len(body) < 50:
            return Failed(
                reason=FailureReason(
                    code="body_too_short",
                    retryable=False,
                    detail=f"rss_body_len={len(body)}",
                )
            )

        published_at = _parse_published_at(entry)
        if published_at is None:
            return Failed(
                reason=FailureReason(
                    code="published_at_missing",
                    retryable=False,
                    detail="rss_pubdate_missing",
                )
            )

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

        try:
            ready = ReadyForArticle(
                title=title,
                body=body,
                published_at=published_at,
                source_id=source_id,
                source_url=source_url,
            )
        except ValueError as e:
            return Failed(
                reason=FailureReason(
                    code="other",
                    retryable=False,
                    detail=f"invariant_violation:{e}",
                )
            )

        authors = _extract_authors(entry)
        primary_author = authors[0] if authors else None

        metadata: dict[str, Any] = {
            "language": feed_language,
            "site_name": self.NAME,
        }
        if primary_author:
            metadata["author"] = primary_author
        if authors:
            metadata["authors"] = list(authors)
        if tags := _extract_tags(entry):
            metadata["tags"] = list(tags)
        if guid := _extract_guid(entry):
            metadata["guid"] = guid

        return FetchedEntry(item=ready, metadata=metadata)
