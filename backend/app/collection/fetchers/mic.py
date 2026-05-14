"""MIC (総務省) 用 Fetcher — Pattern H、RDF (RSS 1.0)、Shift_JIS。

Phase 3 PR 3-h-1。総務省の新着情報 RDF feed (`/news.rdf`) を MEXT 同型の
Pattern H で扱う。Tier 1 ソースで唯一の **Shift_JIS** エンコード。

per-source 設計 (実 RSS 観察ベース、SSoT: tier1-fetcher-research.md §PR 3-h-1):

- feed が **RDF (RSS 1.0)**、宣言が ``<?xml encoding="Shift_JIS"?>``
- ``feedparser.parse(response.content)`` (bytes) 必須。``response.text`` は
  httpx の charset 推定 (UTF-8 fallback) で文字化けが起きる。bytes 経由なら
  feedparser が XML 宣言から正しく Shift_JIS を sniff する
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


def _strip_html(s: str) -> str:
    """HTML タグ + whitespace 正規化 (title clean 用)。"""
    if not s:
        return ""
    return _WHITESPACE_RE.sub(" ", html.unescape(_HTML_TAG_RE.sub(" ", s))).strip()


def _parse_published_at(entry: dict[str, Any]) -> PublishedAt | None:
    """feedparser の ``*_parsed`` (struct_time) を UTC ``PublishedAt`` に変換する。

    Pattern H 固有: 本値が None でも drop しない (HTML 補完を待つ)。
    """
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    if parsed is None:
        return None
    try:
        dt = datetime(*parsed[:6], tzinfo=UTC)
    except (TypeError, ValueError):
        return None
    return PublishedAt(value=dt)


class MICFetcher:
    """MIC 用 Pattern H Fetcher (RDF / RSS 1.0、Shift_JIS)。"""

    NAME: ClassVar[str] = "MIC"
    ENDPOINT_URL: ClassVar[str] = "https://www.soumu.go.jp/news.rdf"

    async def fetch(self, source_id: int) -> AsyncIterator[IncompleteArticle]:
        # Shift_JIS sniff のため bytes で feedparser に渡す
        feed_bytes = await self._fetch_feed()
        feed = await asyncio.to_thread(feedparser.parse, feed_bytes)
        if feed.bozo and not feed.entries:
            logger.warning(
                "mic_feed_parse_error",
                source=self.NAME,
                error=str(feed.bozo_exception),
            )
            raise PermanentFetchError(
                f"feed parse error: {self.NAME}: {feed.bozo_exception}"
            )

        for entry in feed.entries:
            item = self._convert_entry(entry, source_id)
            if item is not None:
                yield item

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
    ) -> IncompleteArticle | None:
        title = _strip_html(entry.get("title", "") or "")
        if not title:
            return None
        title = title[:500]

        link = entry.get("link", "") or ""
        try:
            source_url = SafeUrl(link)
        except ValueError:
            return None

        published_at_hint = _parse_published_at(entry)

        return IncompleteArticle(
            title=title,
            source_id=source_id,
            source_url=source_url,
            published_at_hint=published_at_hint,
        )
