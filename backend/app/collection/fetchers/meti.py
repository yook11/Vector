"""METI (経済産業省) 用 Fetcher (Atom 1.0、UTF-8)。

per-source 設計: Atom 1.0 ルート。``<summary>`` は 300-400 字程度の
リード文のみ。RSS body を信用せず本文は detail HTML 抽出に委譲。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import ClassVar

from app.collection.article.domain.article import ReadyForArticle
from app.collection.fetchers.tools.fetched_article import FetchedArticle
from app.collection.fetchers.tools.passport_builder import try_build_passport
from app.collection.fetchers.tools.rss_parser import RssEntry, RssParser
from app.collection.incomplete_article.domain.incomplete_article import (
    IncompleteArticle,
)


class METIFetcher:
    NAME: ClassVar[str] = "METI"
    ENDPOINT_URL: ClassVar[str] = "https://www.meti.go.jp/ml_index_release_atom.xml"

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


class METIAdapter:
    """METI 用 SourceAdapter (Pattern H、body 不信用)。"""

    NAME = "METI"
    ENDPOINT_URL = "https://www.meti.go.jp/ml_index_release_atom.xml"

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
