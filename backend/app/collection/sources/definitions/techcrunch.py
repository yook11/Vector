"""TechCrunch 用 Source。

TC の RSS feed は ``<description>`` にリード文 (~140 chars) しか含まず
``<content:encoded>`` も提供しないため body は HTML 抽出に委ねる。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import ClassVar

from app.collection.domain.observed_article import ObservedOrigin
from app.collection.domain.source_completion_profile import (
    DEFAULT_PROFILE,
    SourceCompletionProfile,
)
from app.collection.source_fetch.fetched_article import FetchedArticle
from app.collection.source_fetch.reader.rss_reader import RssEntry
from app.collection.source_fetch.tools.fetch_tools import FetchTools
from app.shared.value_objects.source_name import SourceName


class TechCrunchSource:
    """TechCrunch 用 Source。"""

    name: ClassVar[SourceName] = SourceName("TechCrunch")
    endpoint_url: ClassVar[str] = "https://techcrunch.com/feed/"
    observed_origin: ClassVar[ObservedOrigin] = ObservedOrigin.feed
    completion_profile: ClassVar[SourceCompletionProfile] = DEFAULT_PROFILE

    @classmethod
    def to_fetched_article(cls, entry: RssEntry) -> FetchedArticle:
        """RSS body を信用しないため body は採らない。"""
        return FetchedArticle(
            title=entry.title,
            url=entry.link,
            body=None,
            published_at=entry.published,
        )

    @classmethod
    async def collect(cls, tools: FetchTools) -> AsyncIterator[FetchedArticle]:
        entries = await tools.rss.fetch(
            endpoint_url=cls.endpoint_url,
            source_name=str(cls.name),
            parse_mode="text",
        )
        for entry in entries:
            yield cls.to_fetched_article(entry)
