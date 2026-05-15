"""CleanTechnica 用 Fetcher — Pattern H (WordPress 出力)。

per-source 設計: RSS は概要文のみ。本文は後段 HTML 取得 + 抽出に委ねる。
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


class CleanTechnicaFetcher:
    NAME: ClassVar[str] = "CleanTechnica"
    ENDPOINT_URL: ClassVar[str] = "https://cleantechnica.com/feed/"

    def __init__(self, parser: RssParser | None = None) -> None:
        self._parser = parser or RssParser()

    async def fetch(self, source_id: int) -> AsyncIterator[IncompleteArticle]:
        entries = await self._parser.fetch(
            endpoint_url=self.ENDPOINT_URL,
            source_name=self.NAME,
            parse_mode="text",
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
