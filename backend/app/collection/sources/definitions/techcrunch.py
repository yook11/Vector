"""TechCrunch 用 Source。

TC の RSS feed は ``<description>`` にリード文 (~140 chars) しか含まず
``<content:encoded>`` も提供しないため body は HTML 抽出に委ねる。
"""

from __future__ import annotations

from typing import ClassVar

from app.collection.article_acquisition.fetched_article import FetchedArticle
from app.collection.article_acquisition.reader.rss_reader import RssEntry
from app.collection.article_acquisition.tools.reader_tools import ReaderTools
from app.collection.domain.observed_article import ObservedOrigin
from app.collection.sources.article_completion_policy import (
    DEFAULT_POLICY,
    ArticleCompletionPolicy,
)
from app.collection.sources.base_article_source import BaseArticleSource
from app.collection.sources.fetch_cadence import FetchCadence
from app.collection.sources.source_name import SourceName


class TechCrunchSource(BaseArticleSource):
    """TechCrunch 用 Source。"""

    name: ClassVar[SourceName] = SourceName("TechCrunch")
    endpoint_url: ClassVar[str] = "https://techcrunch.com/feed/"
    observed_origin: ClassVar[ObservedOrigin] = ObservedOrigin.feed
    completion_policy: ClassVar[ArticleCompletionPolicy] = DEFAULT_POLICY
    fetch_cadence: ClassVar[FetchCadence] = FetchCadence.HIGH

    @classmethod
    async def read(cls, tools: ReaderTools) -> list[RssEntry]:
        return await tools.rss.fetch(
            endpoint_url=cls.endpoint_url,
            source_name=str(cls.name),
            parse_mode="text",
        )

    @classmethod
    def map_entry(cls, entry: RssEntry) -> FetchedArticle:
        """RSS body を信用しないため body は採らない。"""
        return FetchedArticle(
            title=entry.title,
            url=entry.link,
            body=None,
            published_at=entry.published,
        )
