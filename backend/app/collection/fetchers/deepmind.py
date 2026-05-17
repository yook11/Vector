"""Google DeepMind 用 Fetcher (RSS 2.0、UTF-8)。

per-source 設計: ``<description>`` は短い概要のみで本文は HTML 取得に
委譲する。RSS body を信用せず ``body_candidate=None`` で builder に渡す。
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from app.collection.domain.observed_article import ObservedOrigin
from app.collection.domain.source_completion_profile import DEFAULT_PROFILE
from app.collection.fetchers.tools.fetched_article import FetchedArticle
from app.collection.fetchers.tools.rss_parser import RssParser


class DeepMindAdapter:
    """Google DeepMind 用 SourceAdapter (Pattern H、body 不信用)。"""

    NAME = "Google DeepMind"
    ENDPOINT_URL = "https://deepmind.google/blog/rss.xml"
    observed_origin = ObservedOrigin.feed
    completion_profile = DEFAULT_PROFILE

    def __init__(self, parser: RssParser | None = None) -> None:
        self._parser = parser or RssParser()

    async def collect(self) -> AsyncIterator[FetchedArticle]:
        entries = await self._parser.fetch(
            endpoint_url=self.ENDPOINT_URL,
            source_name=self.NAME,
            parse_mode="bytes",
        )
        for entry in entries:
            yield FetchedArticle(
                title=entry.title,
                url=entry.link,
                body=None,
                published_at=entry.published,
            )
