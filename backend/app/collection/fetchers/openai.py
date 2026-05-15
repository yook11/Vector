"""OpenAI 用 Fetcher (RSS 2.0、UTF-8)。

per-source 設計: ``<description>`` は ~150 chars の短い概要のみで本文は
HTML 詳細ページに委譲する。RSS body を信用しないため
``body_candidate=None`` で builder に渡し Incomplete 経路に固定する。
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from app.collection.fetchers.tools.fetched_article import FetchedArticle
from app.collection.fetchers.tools.rss_parser import RssParser


class OpenAIAdapter:
    """OpenAI 用 SourceAdapter (Pattern H、body 不信用)。"""

    NAME = "OpenAI"
    ENDPOINT_URL = "https://openai.com/news/rss.xml"

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
