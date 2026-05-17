"""SpaceNews 用 Fetcher (WordPress 出力)。

per-source 設計: RSS は ``<description>`` にリード文 + 画像 + truncate
signature のみで full body を出さない。RSS body を信用せず本文は後段
HTML 抽出に委ねる。
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from app.collection.domain.observed_article import ObservedOrigin
from app.collection.domain.source_completion_profile import DEFAULT_PROFILE
from app.collection.fetchers.tools.fetched_article import FetchedArticle
from app.collection.fetchers.tools.rss_parser import RssParser


class SpaceNewsAdapter:
    """SpaceNews 用 SourceAdapter (Pattern H、body 不信用)。"""

    NAME = "SpaceNews"
    ENDPOINT_URL = "https://spacenews.com/feed/"
    observed_origin = ObservedOrigin.feed
    completion_profile = DEFAULT_PROFILE

    def __init__(self, parser: RssParser | None = None) -> None:
        self._parser = parser or RssParser()

    async def collect(self) -> AsyncIterator[FetchedArticle]:
        entries = await self._parser.fetch(
            endpoint_url=self.ENDPOINT_URL,
            source_name=self.NAME,
            parse_mode="text",
        )
        for entry in entries:
            yield FetchedArticle(
                title=entry.title,
                url=entry.link,
                body=None,
                published_at=entry.published,
            )
