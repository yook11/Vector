"""OpenAI 用 Fetcher — Pattern H、RSS 2.0、UTF-8。

per-source 設計: ``<description>`` は ~150 chars の短い概要のみで本文は
HTML 詳細ページに委譲する Pattern H ソース。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import ClassVar

from app.collection.article.domain.value_objects import PublishedAt
from app.collection.fetchers.tools.rss_parser import RssEntry, RssParser
from app.collection.incomplete_article.domain.incomplete_article import (
    IncompleteArticle,
)
from app.shared.value_objects.canonical_article_url import CanonicalArticleUrl


class OpenAIFetcher:
    NAME: ClassVar[str] = "OpenAI"
    ENDPOINT_URL: ClassVar[str] = "https://openai.com/news/rss.xml"

    def __init__(self, parser: RssParser | None = None) -> None:
        self._parser = parser or RssParser()

    async def fetch(self, source_id: int) -> AsyncIterator[IncompleteArticle]:
        entries = await self._parser.fetch(
            endpoint_url=self.ENDPOINT_URL,
            source_name=self.NAME,
            parse_mode="bytes",
        )
        for entry in entries:
            item = self._convert_entry(entry, source_id)
            if item is not None:
                yield item

    def _convert_entry(
        self,
        entry: RssEntry,
        source_id: int,
    ) -> IncompleteArticle | None:
        title = entry.title[:500]
        if not title:
            return None
        try:
            source_url = CanonicalArticleUrl(entry.link)
        except ValueError:
            return None
        published_at_hint = (
            PublishedAt(value=entry.published) if entry.published else None
        )
        return IncompleteArticle(
            title=title,
            source_id=source_id,
            source_url=source_url,
            published_at_hint=published_at_hint,
        )
