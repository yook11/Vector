"""Cornell Chronicle 用 Fetcher — Pattern H、6 taxonomy term feed 巡回。

Cornell Chronicle (``https://news.cornell.edu/``) は学部別の
``/taxonomy/term/<id>/feed`` で AI / Computing / Life Sci / Energy / Phys Sci /
Health 等カテゴリ別 RSS を提供する。本体 ``/news/feed`` は site-wide 雑多な
ため採用せず、対象 6 カテゴリのみを ``FEEDS`` ClassVar で巡回する。

per-source 設計:

- feed が **RSS 2.0** (UTF-8、Drupal 生成器)
- description は短い概要のみ → Pattern H (本文は HTML 取得に委譲)

複数 feed 巡回:

- 6 taxonomy term feed を ``FEEDS`` ClassVar で保持 (NASA fetcher と同設計)
- 1 feed の recoverable な ``ExternalFetchError`` (``RECOVERABLE_FETCH_ERRORS``)
  は warn して次 feed に進む (全停止しない)。非 recoverable な
  ``ExternalFetchError`` は source 全体失敗として伝播する (catch しない)
- in-memory ``seen_urls: set[str]`` で同 cron 周期内の重複 URL を排除
  (1 記事が複数 category に tag されるため、feed 間で URL 重複が発生する)
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import ClassVar

import structlog

from app.collection.fetchers.tools.fetched_article import FetchedArticle
from app.collection.fetchers.tools.http_error_translation import (
    RECOVERABLE_FETCH_ERRORS,
)
from app.collection.fetchers.tools.rss_parser import RssParser

logger = structlog.get_logger(__name__)


class CornellChronicleAdapter:
    """Cornell Chronicle 用 SourceAdapter (Pattern H、6 feed 巡回 + URL dedup)。

    1 feed の recoverable な ``ExternalFetchError`` は ``cornell_feed_skip``
    warning を残して次 feed に進む (旧 ``CornellChronicleFetcher`` と同挙動)。
    非 recoverable な ``ExternalFetchError`` は catch せず source 全体失敗として
    伝播する。
    1 記事が複数 category に tag されるため feed 間 URL 重複を
    in-memory ``seen_urls`` で排除する。
    """

    NAME = "Cornell Chronicle"
    ENDPOINT_URL = "https://news.cornell.edu/taxonomy/term/24043/feed"
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

    def __init__(self, parser: RssParser | None = None) -> None:
        self._parser = parser or RssParser()

    async def collect(self) -> AsyncIterator[FetchedArticle]:
        seen_urls: set[str] = set()
        for feed_url in self.FEEDS:
            try:
                entries = await self._parser.fetch(
                    endpoint_url=feed_url,
                    source_name=self.NAME,
                    parse_mode="bytes",
                )
            except RECOVERABLE_FETCH_ERRORS as e:
                # 1 feed の recoverable 失敗で全停止しない (他 feed は続行)
                logger.warning(
                    "cornell_feed_skip",
                    source=self.NAME,
                    feed=feed_url,
                    error=str(e),
                )
                continue
            # 非 recoverable は catch しない (source 全体失敗として伝播)
            for entry in entries:
                if not entry.link or entry.link in seen_urls:
                    continue
                seen_urls.add(entry.link)
                yield FetchedArticle(
                    title=entry.title,
                    url=entry.link,
                    body=None,
                    published_at=entry.published,
                )
