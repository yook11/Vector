"""Google DeepMind 用 Source (RSS 2.0、UTF-8)。

per-source 設計: ``<description>`` は短い概要のみで本文は HTML 取得に
委譲する。RSS body を信用せず ``body=None`` で yield する。
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


class DeepMindSource:
    """Google DeepMind 用 ``XxxSource`` (Pattern H、body 不信用)。"""

    name: ClassVar[SourceName] = SourceName("Google DeepMind")
    endpoint_url: ClassVar[str] = "https://deepmind.google/blog/rss.xml"
    observed_origin: ClassVar[ObservedOrigin] = ObservedOrigin.feed
    completion_profile: ClassVar[SourceCompletionProfile] = DEFAULT_PROFILE

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
