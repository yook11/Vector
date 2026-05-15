"""TechCrunch 用 Fetcher。

per-source 設計: TC の RSS feed は ``<description>`` にリード文 (~140 chars)
しか含まず ``<content:encoded>`` も提供しない (`spec
collection-source-rss-research.md`)。Fetcher は **RSS 本文を信用しない** —
``body_candidate=None`` を builder に渡し、URL + title を ``IncompleteArticle``
として yield する。後段の ``ArticleCompletionService`` が HTML 本文を取得 +
promotion する 2 段構成。

将来 TC が ``<content:encoded>`` に full body を載せるようになった場合、
``_pick_body`` 相当を持たせて builder に body を渡せば自然に Ready 経路に
切り替わる。

HTTP 取得 / feedparser / SSRF guard / title plain text 正規化は L2
``RssParser`` に集約済。本ファイルは L3 翻訳 (RssEntry → passport) の
per-source 責務だけを持つ。
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


class TechCrunchFetcher:
    """TechCrunch 用 Fetcher。"""

    NAME: ClassVar[str] = "TechCrunch"
    ENDPOINT_URL: ClassVar[str] = "https://techcrunch.com/feed/"

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
        """1 entry を passport に変換する。TC は RSS body を信用しないため
        ``body_candidate=None`` で builder に渡し、Incomplete 経路に固定する。"""
        return try_build_passport(
            title=entry.title,
            link=entry.link,
            body_candidate=None,
            published_hint=entry.published,
            source_id=source_id,
        )
