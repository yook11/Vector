"""Cornell Chronicle 用 Fetcher — Pattern H、6 taxonomy term feed 巡回。

Phase 3 PR 3-e。Cornell Chronicle (`https://news.cornell.edu/`) は学部別の
``/taxonomy/term/<id>/feed`` で AI / Computing / Life Sci / Energy / Phys Sci /
Health 等カテゴリ別 RSS を提供する。本体 ``/news/feed`` は site-wide 雑多な
ため採用せず、対象 6 カテゴリのみを ``FEEDS`` ClassVar で巡回する。

per-source 設計 (実 RSS 観察ベース):

- feed が **RSS 2.0** (UTF-8、Drupal 生成器)
- description は短い概要のみ → Pattern H (本文は HTML 取得に委譲)

複数 feed 巡回:

- 6 taxonomy term feed を ``FEEDS`` ClassVar で保持 (NASA fetcher と同設計)
- 1 feed の TemporaryFetchError は warn して次 feed に進む (全停止しない)
- in-memory ``seen_urls: set[str]`` で同 cron 周期内の重複 URL を排除
  (1 記事が複数 category に tag されるため、feed 間で URL 重複が発生する)
- DB レイヤの ``articles.url`` UNIQUE + ``on_conflict_do_nothing()`` も二段
  防御として効く (worker 間 race 用)
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


class CornellChronicleFetcher:
    """Cornell Chronicle 用 FEEDS 巡回 Pattern H Fetcher。

    ``ENDPOINT_URL`` は ``news_sources.endpoint_url`` 列との互換のため代表値
    (AI feed) を残すが、実 fetch は ``FEEDS`` の 6 URL を順次巡回する。
    """

    NAME: ClassVar[str] = "Cornell Chronicle"
    ENDPOINT_URL: ClassVar[str] = "https://news.cornell.edu/taxonomy/term/24043/feed"
    FEEDS: ClassVar[tuple[str, ...]] = (
        # Artificial Intelligence
        "https://news.cornell.edu/taxonomy/term/24043/feed",
        # Computing & Information Sciences
        "https://news.cornell.edu/taxonomy/term/14256/feed",
        # Life Sciences & Veterinary Medicine
        "https://news.cornell.edu/taxonomy/term/15056/feed",
        # Energy, Environment & Sustainability
        "https://news.cornell.edu/taxonomy/term/15621/feed",
        # Physical Sciences & Engineering
        "https://news.cornell.edu/taxonomy/term/14252/feed",
        # Health, Nutrition & Medicine
        "https://news.cornell.edu/taxonomy/term/14248/feed",
    )

    async def fetch(self, source_id: int) -> AsyncIterator[IncompleteArticle]:
        seen_urls: set[str] = set()
        for feed_url in self.FEEDS:
            try:
                feed_bytes = await self._fetch_feed(feed_url)
            except TemporaryFetchError as e:
                # 1 feed の transient 失敗で全停止しない (他 feed は続行)
                logger.warning(
                    "cornell_feed_skip",
                    source=self.NAME,
                    feed=feed_url,
                    error=str(e),
                )
                continue
            feed = await asyncio.to_thread(feedparser.parse, feed_bytes)
            if feed.bozo and not feed.entries:
                logger.warning(
                    "cornell_feed_parse_error",
                    source=self.NAME,
                    feed=feed_url,
                    error=str(feed.bozo_exception),
                )
                continue

            for entry in feed.entries:
                link = (entry.get("link") or "").strip()
                if not link or link in seen_urls:
                    continue
                seen_urls.add(link)
                item = self._convert_entry(entry, source_id)
                if item is not None:
                    yield item

    async def _fetch_feed(self, url: str) -> bytes:
        async with make_safe_async_client(
            headers={"User-Agent": _USER_AGENT},
            verify=True,
            timeout=_HTTP_TIMEOUT,
        ) as client:
            try:
                response = await client.get(url)
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
