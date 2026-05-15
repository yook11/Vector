"""JPCERT/CC 用 Fetcher (RDF / RSS 1.0)。

per-source 設計: feed が RDF (RSS 1.0) ルート (``<rdf:RDF>``)。``<title>``
は多行 + インデント空白を含むため ``RssParser.normalize_entry`` の whitespace
正規化で吸収する。RSS body を信用せず本文は HTML 抽出に委譲。
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


class JPCERTFetcher:
    NAME: ClassVar[str] = "JPCERT/CC"
    ENDPOINT_URL: ClassVar[str] = "https://www.jpcert.or.jp/rss/jpcert.rdf"

    def __init__(self, parser: RssParser | None = None) -> None:
        self._parser = parser or RssParser()

    async def fetch(
        self, source_id: int
    ) -> AsyncIterator[ReadyForArticle | IncompleteArticle]:
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
    ) -> ReadyForArticle | IncompleteArticle | None:
        return try_build_passport(
            title=entry.title,
            link=entry.link,
            body_candidate=None,
            published_hint=entry.published,
            source_id=source_id,
        )
