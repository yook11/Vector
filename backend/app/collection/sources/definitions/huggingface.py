"""Hugging Face Blog 用 Source。

HF 公式 blog は ``<description>`` を空文字で出力するため body は HTML 抽出に
委ねる。
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


class HuggingFaceBlogSource(BaseArticleSource):
    """Hugging Face Blog 用 Source。"""

    name: ClassVar[SourceName] = SourceName("Hugging Face")
    endpoint_url: ClassVar[str] = "https://huggingface.co/blog/feed.xml"
    observed_origin: ClassVar[ObservedOrigin] = ObservedOrigin.feed
    completion_policy: ClassVar[ArticleCompletionPolicy] = DEFAULT_POLICY
    fetch_cadence: ClassVar[FetchCadence] = FetchCadence.MEDIUM

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
