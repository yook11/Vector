"""OpenAI 用 Source (RSS 2.0、UTF-8)。

``<description>`` は ~150 chars の短い概要のみで本文を含まないため body は
HTML 詳細ページに委ねる。
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


class OpenAISource:
    """OpenAI 用 Source。"""

    name: ClassVar[SourceName] = SourceName("OpenAI")
    endpoint_url: ClassVar[str] = "https://openai.com/news/rss.xml"
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
