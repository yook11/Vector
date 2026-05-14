"""NASA 用 Fetcher — Pattern R (RSS-only)、複数 feed 巡回 + URL dedup。

collection-acquisition-redesign Phase 1c-A1 で per-source 設計を確立、Phase 3
PR 3-i-2 で本体 ``/feed/`` に加えて 5 補強 feed を ``FEEDS`` ClassVar で巡回
する設計へ拡張。

per-source 設計:

- body は ``entry.content[0].value`` を**直取り** (nav noise 含むまま)

複数 feed 巡回 (PR 3-i-2):

- 6 feed (本体 + news-release / technology / aeronautics / station / artemis)
  を ``FEEDS`` ClassVar で保持
- ``fetch()`` で順次 GET → 1 feed の TemporaryFetchError は warn して次 feed
  に進む (全停止しない)
- in-memory ``seen_urls: set[str]`` で同 cron 周期内の重複 URL を排除
  (本体 feed と news-release/artemis 等で URL が重複するため)
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

from app.collection.article.domain.article import ReadyForArticle
from app.collection.article.domain.value_objects import PublishedAt
from app.collection.errors import PermanentFetchError, TemporaryFetchError
from app.shared.security.safe_http import make_safe_async_client
from app.shared.security.ssrf_guard import HostBlockedError, HostResolutionError
from app.shared.value_objects.safe_url import SafeUrl

logger = structlog.get_logger(__name__)

_USER_AGENT = "Mozilla/5.0 (compatible; Vector/1.0; +https://github.com/yook11/Vector)"
_HTTP_TIMEOUT = httpx.Timeout(connect=5.0, read=30.0, write=10.0, pool=5.0)
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"\s+")


def _strip_html(s: str) -> str:
    """HTML タグを剥がして plain text に正規化する。

    NASA の ``<content:encoded>`` は本文前後に nav menu / boilerplate を含むが、
    本関数は HTML タグ除去のみを行い nav noise の除去は行わない (Phase 1
    では Stage 2 LLM 側で吸収する設計)。
    """
    if not s:
        return ""
    return _WHITESPACE_RE.sub(" ", html.unescape(_HTML_TAG_RE.sub(" ", s))).strip()


def _extract_body(entry: dict[str, Any]) -> str:
    """``<content:encoded>`` を直取り (nav noise 含むまま)。"""
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


class NASAFetcher:
    """NASA 用 RSS-only Fetcher (本体 + 5 補強 feed 巡回)。

    ``ENDPOINT_URL`` は ``news_sources.endpoint_url`` 列との互換のため本体
    ``/feed/`` を representative 値として残すが、実際の fetch は ``FEEDS``
    の 6 URL を順次巡回する。
    """

    NAME: ClassVar[str] = "NASA"
    ENDPOINT_URL: ClassVar[str] = "https://www.nasa.gov/feed/"
    FEEDS: ClassVar[tuple[str, ...]] = (
        "https://www.nasa.gov/feed/",
        "https://www.nasa.gov/news-release/feed/",
        "https://www.nasa.gov/technology/feed/",
        "https://www.nasa.gov/aeronautics/feed/",
        "https://www.nasa.gov/missions/station/feed/",
        "https://www.nasa.gov/missions/artemis/feed/",
    )

    async def fetch(self, source_id: int) -> AsyncIterator[ReadyForArticle]:
        seen_urls: set[str] = set()
        for feed_url in self.FEEDS:
            try:
                feed_text = await self._fetch_feed(feed_url)
            except TemporaryFetchError as e:
                # 1 feed の transient 失敗で全停止しない (他 feed は続行)
                logger.warning(
                    "nasa_feed_skip",
                    source=self.NAME,
                    feed=feed_url,
                    error=str(e),
                )
                continue
            feed = await asyncio.to_thread(feedparser.parse, feed_text)
            if feed.bozo and not feed.entries:
                logger.warning(
                    "nasa_feed_parse_error",
                    source=self.NAME,
                    feed=feed_url,
                    error=str(feed.bozo_exception),
                )
                continue

            for entry in feed.entries:
                link = (entry.get("link") or "").strip()
                if link and link in seen_urls:
                    continue
                if link:
                    seen_urls.add(link)
                item = self._convert_entry(entry, source_id)
                if item is not None:
                    yield item

    async def _fetch_feed(self, url: str) -> str:
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
            return response.text

    def _convert_entry(
        self,
        entry: dict[str, Any],
        source_id: int,
    ) -> ReadyForArticle | None:
        title = _strip_html(entry.get("title", "") or "")
        if not title:
            return None
        title = title[:500]

        body = _strip_html(_extract_body(entry))
        if len(body) < 50:
            return None

        published_at = _parse_published_at(entry)
        if published_at is None:
            return None

        link = entry.get("link", "") or ""
        try:
            source_url = SafeUrl(link)
        except ValueError:
            return None

        try:
            return ReadyForArticle(
                title=title,
                body=body,
                published_at=published_at,
                source_id=source_id,
                source_url=source_url,
            )
        except ValueError:
            return None
