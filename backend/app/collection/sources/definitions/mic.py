"""MIC (総務省) 用 Source (RDF / RSS 1.0、Shift_JIS)。

feed が ``<?xml encoding="Shift_JIS"?>`` のため ``parse_mode="bytes"`` で
feedparser に Shift_JIS を sniff させる (``response.text`` 経由だと httpx の
charset 推定で文字化けする)。body は HTML 抽出に委ねる。
"""

from __future__ import annotations

from typing import ClassVar

from app.collection.article_collection.fetched_article import FetchedArticle
from app.collection.article_collection.reader.rss_reader import RssEntry
from app.collection.article_collection.tools.reader_tools import ReaderTools
from app.collection.domain.observed_article import ObservedOrigin
from app.collection.sources.article_completion_policy import (
    DEFAULT_POLICY,
    ArticleCompletionPolicy,
)
from app.collection.sources.base_article_source import BaseArticleSource
from app.shared.value_objects.source_name import SourceName


class MICSource(BaseArticleSource):
    """MIC 用 Source (Shift_JIS feed)。"""

    name: ClassVar[SourceName] = SourceName("MIC")
    endpoint_url: ClassVar[str] = "https://www.soumu.go.jp/news.rdf"
    observed_origin: ClassVar[ObservedOrigin] = ObservedOrigin.feed
    completion_policy: ClassVar[ArticleCompletionPolicy] = DEFAULT_POLICY

    @classmethod
    async def read(cls, tools: ReaderTools) -> list[RssEntry]:
        return await tools.rss.fetch(
            endpoint_url=cls.endpoint_url,
            source_name=str(cls.name),
            parse_mode="bytes",
        )

    @classmethod
    def map_entry(cls, entry: RssEntry) -> FetchedArticle:
        return FetchedArticle(
            title=entry.title,
            url=entry.link,
            body=None,
            published_at=entry.published,
        )
