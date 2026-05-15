"""MIC (総務省) 用 Fetcher (RDF / RSS 1.0、Shift_JIS)。

per-source 設計: feed が RDF (RSS 1.0) 宣言で ``<?xml encoding="Shift_JIS"?>``。
``parse_mode="bytes"`` を選ぶことで feedparser が XML 宣言から Shift_JIS を
sniff できる (``response.text`` 経由だと httpx の charset 推定で文字化けする
ため)。RSS body を信用せず本文は HTML 抽出に委譲。
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


class MICFetcher:
    NAME: ClassVar[str] = "MIC"
    ENDPOINT_URL: ClassVar[str] = "https://www.soumu.go.jp/news.rdf"

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


class MICAdapter:
    """MIC 用 SourceAdapter (Pattern H、body 不信用、Shift_JIS feed)。

    ``parse_mode="bytes"`` で feedparser に encoding sniff を任せる。
    """

    NAME = "MIC"
    ENDPOINT_URL = "https://www.soumu.go.jp/news.rdf"

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
