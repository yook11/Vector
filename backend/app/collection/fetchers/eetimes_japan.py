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

    NAME = "EE Times Japan"
    ENDPOINT_URL = "https://rss.itmedia.co.jp/rss/2.0/eetimes.xml"

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
