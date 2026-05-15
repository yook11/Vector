"""ITmedia NEWS 用 Fetcher (本文は HTML 必須)。

per-source 設計: RSS は ~100 chars のリード文のみ。RSS body を信用せず
後段 HTML 抽出に委ねる。
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from app.collection.fetchers.tools.fetched_article import FetchedArticle
from app.collection.fetchers.tools.rss_parser import RssParser


class ITmediaNewsAdapter:
    """ITmedia NEWS 用 SourceAdapter (Pattern H、body 不信用)。"""

    NAME = "ITmedia NEWS"
    ENDPOINT_URL = "https://rss.itmedia.co.jp/rss/2.0/news_bursts.xml"

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
