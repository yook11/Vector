"""JPCERT/CC 用 Source (RDF / RSS 1.0)。

feed が RDF (RSS 1.0) ルート (``<rdf:RDF>``)。``<title>`` は多行 +
インデント空白を含む。body は HTML 抽出に委ねる。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import ClassVar

from app.collection.domain.observed_article import ObservedOrigin
from app.collection.source_fetch.fetched_article import FetchedArticle
from app.collection.source_fetch.tools.fetch_tools import FetchTools
from app.collection.sources.article_completion_policy import (
    DEFAULT_POLICY,
    ArticleCompletionPolicy,
)
from app.shared.value_objects.source_name import SourceName


class JPCERTSource:
    """JPCERT/CC 用 Source。"""

    name: ClassVar[SourceName] = SourceName("JPCERT/CC")
    endpoint_url: ClassVar[str] = "https://www.jpcert.or.jp/rss/jpcert.rdf"
    observed_origin: ClassVar[ObservedOrigin] = ObservedOrigin.feed
    completion_policy: ClassVar[ArticleCompletionPolicy] = DEFAULT_POLICY

    @classmethod
    async def collect(cls, tools: FetchTools) -> AsyncIterator[FetchedArticle]:
        entries = await tools.rss.fetch(
            endpoint_url=cls.endpoint_url,
            source_name=str(cls.name),
            parse_mode="text",
        )
        for entry in entries:
            yield FetchedArticle(
                title=entry.title,
                url=entry.link,
                body=None,
                published_at=entry.published,
            )
