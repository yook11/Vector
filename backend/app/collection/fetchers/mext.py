"""MEXT (文部科学省) 用 Fetcher (RDF / RSS 1.0、UTF-8)。

per-source 設計: RDF (RSS 1.0) ルート。``<description>`` は空であることが
多く、RSS body を信用せず本文は HTML 抽出に委譲。
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from app.collection.domain.observed_article import ObservedOrigin
from app.collection.domain.source_completion_profile import DEFAULT_PROFILE
from app.collection.fetchers.tools.fetched_article import FetchedArticle
from app.collection.fetchers.tools.rss_parser import RssParser


class MEXTAdapter:
    """MEXT 用 SourceAdapter (Pattern H、body 不信用)。"""

    NAME = "MEXT"
    ENDPOINT_URL = "https://www.mext.go.jp/b_menu/news/index.rdf"
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
