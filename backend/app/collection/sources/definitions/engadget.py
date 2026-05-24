"""Engadget 用 Source。

RSS の ``<content:encoded>`` は ~50 chars の caption 程度で本文ではないため
body は HTML 抽出に委ねる。
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
from app.shared.value_objects.source_name import SourceName


class EngadgetSource(BaseArticleSource):
    """Engadget 用 Source。"""

    name: ClassVar[SourceName] = SourceName("Engadget")
    endpoint_url: ClassVar[str] = "https://www.engadget.com/rss.xml"
    observed_origin: ClassVar[ObservedOrigin] = ObservedOrigin.feed
    completion_policy: ClassVar[ArticleCompletionPolicy] = DEFAULT_POLICY

    @classmethod
    async def read(cls, tools: ReaderTools) -> list[RssEntry]:
        return await tools.rss.fetch(
            endpoint_url=cls.endpoint_url,
            source_name=str(cls.name),
            parse_mode="text",
        )

    @classmethod
    def map_entry(cls, entry: RssEntry) -> FetchedArticle:
        return FetchedArticle(
            title=entry.title,
            url=entry.link,
            body=None,
            published_at=entry.published,
        )
