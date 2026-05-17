"""EE Times Japan 用 Fetcher (本文は HTML 必須)。

per-source 設計: RSS は ~150 chars のリード文のみで本文欠落。RSS body を
信用せず後段 HTML 抽出 (trafilatura) に委ねる。
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from app.collection.fetchers.tools.fetched_article import FetchedArticle
from app.collection.fetchers.tools.rss_parser import RssParser


class EETimesJapanAdapter:
    """EE Times Japan 用 SourceAdapter (Pattern H、body 不信用)。"""

    def __init__(
        self,
        *,
        endpoint_url: str,
        source_name: str,
        parser: RssParser | None = None,
    ) -> None:
        self._endpoint_url = endpoint_url
        self._source_name = source_name
        self._parser = parser or RssParser()

    async def collect(self) -> AsyncIterator[FetchedArticle]:
        entries = await self._parser.fetch(
            endpoint_url=self._endpoint_url,
            source_name=self._source_name,
            parse_mode="text",
        )
        for entry in entries:
            yield FetchedArticle(
                title=entry.title,
                url=entry.link,
                body=None,
                published_at=entry.published,
            )
