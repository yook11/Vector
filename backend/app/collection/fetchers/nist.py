"""NIST 用 Fetcher (RSS 2.0、UTF-8)。

per-source 設計: description は短い概要 (~80 chars) で RSS body を信用せず
本文は HTML 抽出に委譲。License は 17 U.S.C. §105 (Public Domain)。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import ClassVar

from app.collection.article.domain.article import ReadyForArticle
from app.collection.fetchers.tools.passport_builder import try_build_passport
from app.collection.fetchers.tools.rss_parser import RssEntry, RssParser
from app.collection.incomplete_article.domain.incomplete_article import (
    IncompleteArticle,
)


class NISTFetcher:
    NAME: ClassVar[str] = "NIST"
    ENDPOINT_URL: ClassVar[str] = "https://www.nist.gov/news-events/news/rss.xml"

    def __init__(self, parser: RssParser | None = None) -> None:
        self._parser = parser or RssParser()

    async def fetch(
        self, source_id: int
    ) -> AsyncIterator[ReadyForArticle | IncompleteArticle]:
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
    ) -> ReadyForArticle | IncompleteArticle | None:
        return try_build_passport(
            title=entry.title,
            link=entry.link,
            body_candidate=None,
            published_hint=entry.published,
            source_id=source_id,
        )
