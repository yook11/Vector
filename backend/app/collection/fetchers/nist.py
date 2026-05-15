"""NIST 用 Fetcher (RSS 2.0、UTF-8)。

per-source 設計: description は短い概要 (~80 chars) で RSS body を信用せず
本文は HTML 抽出に委譲。License は 17 U.S.C. §105 (Public Domain)。
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from app.collection.fetchers.tools.fetched_article import FetchedArticle
from app.collection.fetchers.tools.rss_parser import RssParser


class NISTAdapter:
    """NIST 用 SourceAdapter (Pattern H、body 不信用)。"""

    NAME = "NIST"
    ENDPOINT_URL = "https://www.nist.gov/news-events/news/rss.xml"

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
