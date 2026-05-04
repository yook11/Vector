"""Cornell Chronicle 用 Fetcher — Pattern H、6 taxonomy term feed 巡回。

Phase 3 PR 3-e。Cornell Chronicle (`https://news.cornell.edu/`) は学部別の
``/taxonomy/term/<id>/feed`` で AI / Computing / Life Sci / Energy / Phys Sci /
Health 等カテゴリ別 RSS を提供する。本体 ``/news/feed`` は site-wide 雑多な
ため採用せず、対象 6 カテゴリのみを ``FEEDS`` ClassVar で巡回する。

per-source 設計 (実 RSS 観察ベース):

- feed が **RSS 2.0** (UTF-8、Drupal 生成器)
- ``<item>`` は ``<title>`` / ``<link>`` (絶対 URL) / ``<guid isPermaLink="true">``
  (link と同値) / ``<pubDate>`` (RFC 822、EDT/EST 含) / ``<description>``
  (1-2 sentences、< 350 chars) を提供
- ``<dc:creator>`` は **Drupal 内部 ID** (``kah53``, ``gs775`` 等) のため
  ``metadata.author = None`` で drop (人間名でないため UI で意味を持たない)
- ``<thumbnail_*>`` / ``<image_featured>`` 等の画像要素を提供 → ``image_featured``
  を最優先で ``metadata.image_url`` に詰める (Tier 1 昇格対象)
- ``<media:content>`` は提供されない、Cornell 独自の ``image_*`` 要素のみ
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
    """``<guid>`` (= URL) を取り出す。"""
    raw = entry.get("id") or entry.get("guid")
    if isinstance(raw, str) and raw:
        return raw[:2048]
    return None


def _extract_image_url(entry: dict[str, Any]) -> SafeUrl | None:
    """Cornell Drupal 独自要素から画像 URL を取り出す。

    優先順位: ``image_featured`` (story_thumbnail_home サイズ) →
    ``thumbnail_360x360`` → ``thumbnail_120x90`` → ``thumbnail_85x85``。
    feedparser は非標準要素を element 名そのままで dict key に格納する。
    """
    for key in (
        "image_featured",
        "thumbnail_360x360",
        "thumbnail_120x90",
        "thumbnail_85x85",
    ):
        raw = entry.get(key)
        if not isinstance(raw, str) or not raw:
            continue
        try:
            return SafeUrl(raw)
        except ValueError:
            continue
    return None


def _normalize_language(raw: str | None) -> str:
    """``en`` / ``en-US`` の表記揺れを統一。"""
    value = (raw or _DEFAULT_LANGUAGE).replace("_", "-")
    return value[:20]


class CornellChronicleFetcher:
    """Cornell Chronicle 用 FEEDS 巡回 Pattern H Fetcher。

    PROVIDES に列挙したフィールドは feed-level / RSS 仕様で 100% 提供される
    前提:

    - ``language``: feed-level ``<language>en</language>``
    - ``guid``: ``<guid isPermaLink="true">`` (link と同値、安定 ID)
    - ``site_name``: hardcode "Cornell Chronicle"

    ``image_url`` は Drupal 独自要素由来で大半の entry で埋まるが probabilistic
    (PROVIDES 非含)。``author`` は内部 ID のため None 固定 (PROVIDES 非含)。

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
    PROVIDES: ClassVar[frozenset[str]] = frozenset({"language", "guid", "site_name"})

    async def fetch(self, source_id: int) -> AsyncIterator[FetchOutcome]:
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

            feed_language = _normalize_language(feed.feed.get("language"))

            for entry in feed.entries:
                link = (entry.get("link") or "").strip()
                if not link or link in seen_urls:
                    continue
                seen_urls.add(link)
                yield self._convert_entry(entry, source_id, feed_language)

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

        metadata: dict[str, Any] = {
            "language": feed_language,
            "site_name": self.NAME,
        }
        if image_url := _extract_image_url(entry):
            metadata["image_url"] = str(image_url)
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
