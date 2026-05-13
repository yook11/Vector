"""Microsoft Research 用 Fetcher — Pattern R (RSS-only)。

collection-acquisition-redesign Phase 1c-A2。RSS feed の ``<content:encoded>``
に full body (7000-65000 chars) を含むが末尾に固定 footer ("Opens in a new
tab The post {title} appeared first on Microsoft Research.") がつくため、
``_strip_html`` 後に per-source 定数 ``_FOOTER_RE`` で除去する。

per-source 設計:

- body は ``entry.content[0].value`` 直取り **+ footer regex strip**
- author は ``entry.author`` の comma-separated string (200 chars cap)
- authors は raw string を ", " で split した tuple
- image は **``None`` 直書き** — 本ソースの RSS は ``<media:content>`` を
  提供しない (og:image は HTML 限定で本 Pattern R では取得しない)
- language は ``feed.feed.language`` (``<channel>/<language>`` 提供あり)

旧 ``fetchers/rss/microsoft_research.py`` (BaseRssFetcher 継承の薄スタブ) は
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

# WordPress 由来の固定 footer。``_strip_html`` 後の plain text 末尾に付く。
# 全 entry で観察、``\s*`` で前後空白を吸収する。
_FOOTER_RE = re.compile(
    r"\s*Opens in a new tab\s*The post .* appeared first on Microsoft Research\.\s*$",
    re.DOTALL,
)


def _strip_html(s: str) -> str:
    """HTML タグを剥がして plain text に正規化する。"""
    if not s:
        return ""
    return _WHITESPACE_RE.sub(" ", html.unescape(_HTML_TAG_RE.sub(" ", s))).strip()


def _strip_footer(body: str) -> str:
    """末尾の固定 boilerplate を per-source regex で除去する。"""
    return _FOOTER_RE.sub("", body)


def _extract_body(entry: dict[str, Any]) -> str:
    """``<content:encoded>`` を直取り。footer 除去は呼び出し側 (``_strip_footer``)。"""
    contents = entry.get("content")
    if isinstance(contents, list) and contents:
        first = contents[0]
        if isinstance(first, dict):
            value = first.get("value")
            if isinstance(value, str):
                return value
    return ""


def _extract_authors_from_csv(raw: str | None) -> tuple[str, ...]:
    """RSS が comma-separated 文字列で出す多著者を tuple 化する。"""
    if not isinstance(raw, str) or not raw:
        return ()
    return tuple(s.strip() for s in raw.split(",") if s.strip())


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


def _normalize_language(raw: str | None) -> str:
    """``en_US`` / ``en-us`` / ``en-US`` の表記揺れを ``en-US`` 系に統一。"""
    value = (raw or _DEFAULT_LANGUAGE).replace("_", "-")
    return value[:20]


class MicrosoftResearchFetcher:
    """Microsoft Research 用 RSS-only Fetcher。

    本ソースは ``<media:content>`` を提供しないため image_url は構造的に
    常に ``None``。footer は per-source 定数 ``_FOOTER_RE`` で除去する
    (regex match しなければ no-op、boilerplate がそのまま残るのは Stage 2
    LLM 吸収範囲、logfire で検知)。
    """

    NAME: ClassVar[str] = "Microsoft Research"
    ENDPOINT_URL: ClassVar[str] = "https://www.microsoft.com/en-us/research/feed/"
    PROVIDES: ClassVar[frozenset[str]] = frozenset({"language", "guid", "site_name"})

    async def fetch(self, source_id: int) -> AsyncIterator[FetchOutcome]:
        feed_text = await self._fetch_feed()
        feed = await asyncio.to_thread(feedparser.parse, feed_text)
        if feed.bozo and not feed.entries:
            logger.warning(
                "microsoft_research_feed_parse_error",
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

        # body: HTML strip → footer strip の順 (footer は plain text 末尾)
        body = _strip_footer(_strip_html(_extract_body(entry)))
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

        raw_author = entry.get("author")
        author = (
            raw_author[:200] if isinstance(raw_author, str) and raw_author else None
        )
        authors = _extract_authors_from_csv(raw_author)

        metadata: dict[str, Any] = {
            "language": feed_language,
            "site_name": self.NAME,
        }
        if author:
            metadata["author"] = author
        if authors:
            metadata["authors"] = list(authors)
        if tags := _extract_tags(entry):
            metadata["tags"] = list(tags)
        if guid := _extract_guid(entry):
            metadata["guid"] = guid

        return FetchedEntry(item=ready, metadata=metadata)
