"""JPCERT/CC 用 Source (RDF / RSS 1.0)。

per-source 設計: feed が RDF (RSS 1.0) ルート (``<rdf:RDF>``)。``<title>``
は多行 + インデント空白を含むため ``RssParser.normalize_entry`` の whitespace
正規化で吸収する。RSS body を信用せず本文は HTML 抽出に委譲。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import ClassVar

from app.collection.domain.observed_article import ObservedOrigin
from app.collection.domain.source_completion_profile import (
    DEFAULT_PROFILE,
    SourceCompletionProfile,
)
from app.collection.fetchers.tools.fetch_tools import FetchTools
from app.collection.fetchers.tools.fetched_article import FetchedArticle
from app.shared.value_objects.source_name import SourceName


class JPCERTSource:
    """JPCERT/CC 用 ``XxxSource`` (Pattern H、body 不信用)。"""

    name: ClassVar[SourceName] = SourceName("JPCERT/CC")
    endpoint_url: ClassVar[str] = "https://www.jpcert.or.jp/rss/jpcert.rdf"
    observed_origin: ClassVar[ObservedOrigin] = ObservedOrigin.feed
    completion_profile: ClassVar[SourceCompletionProfile] = DEFAULT_PROFILE

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
