"""Engadget 用 Fetcher (本文は HTML 必須)。

per-source 設計: RSS の ``<content:encoded>`` は ~50 chars の caption
程度で本文ではない。RSS body を信用せず本文は後段 HTML 抽出
(trafilatura) に委ねる。
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from app.collection.fetchers.tools.fetched_article import FetchedArticle
from app.collection.fetchers.tools.rss_parser import RssParser


class EngadgetAdapter:
    """Engadget 用 SourceAdapter (Pattern H、body 不信用)。"""

    NAME = "Engadget"
    ENDPOINT_URL = "https://www.engadget.com/rss.xml"

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
