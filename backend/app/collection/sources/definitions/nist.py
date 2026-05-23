"""NIST 用 Source (RSS 2.0、UTF-8)。

description は短い概要 (~80 chars) のみで body は HTML 抽出に委ねる。
License は 17 U.S.C. §105 (Public Domain)。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import ClassVar

from app.collection.article_collection.fetched_article import FetchedArticle
from app.collection.article_collection.tools.fetch_tools import FetchTools
from app.collection.domain.observed_article import ObservedOrigin
from app.collection.sources.article_completion_policy import (
    DEFAULT_POLICY,
    ArticleCompletionPolicy,
)
from app.shared.value_objects.source_name import SourceName


class NISTSource:
    """NIST 用 Source。"""

    name: ClassVar[SourceName] = SourceName("NIST")
    endpoint_url: ClassVar[str] = "https://www.nist.gov/news-events/news/rss.xml"
    observed_origin: ClassVar[ObservedOrigin] = ObservedOrigin.feed
    completion_policy: ClassVar[ArticleCompletionPolicy] = DEFAULT_POLICY

    @classmethod
    async def collect(cls, tools: FetchTools) -> AsyncIterator[FetchedArticle]:
        entries = await tools.rss.fetch(
            endpoint_url=cls.endpoint_url,
            source_name=str(cls.name),
            parse_mode="bytes",
        )
        for entry in entries:
            yield FetchedArticle(
                title=entry.title,
                url=entry.link,
                body=None,
                published_at=entry.published,
            )
