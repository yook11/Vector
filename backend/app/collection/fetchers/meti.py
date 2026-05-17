"""METI (経済産業省) 用 Fetcher (Atom 1.0、UTF-8)。

per-source 設計: Atom 1.0 ルート。``<summary>`` は 300-400 字程度の
リード文のみ。RSS body を信用せず本文は detail HTML 抽出に委譲。
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from app.collection.fetchers.tools.fetched_article import FetchedArticle
from app.collection.fetchers.tools.rss_parser import RssParser


class METIAdapter:
    """METI 用 SourceAdapter (Pattern H、body 不信用)。"""

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
            parse_mode="bytes",
        )
        for entry in entries:
            yield FetchedArticle(
                title=entry.title,
                url=entry.link,
                body=None,
                published_at=entry.published,
            )
