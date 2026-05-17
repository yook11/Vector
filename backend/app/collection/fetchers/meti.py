"""METI (経済産業省) 用 Source (Atom 1.0、UTF-8)。

per-source 設計: Atom 1.0 ルート。``<summary>`` は 300-400 字程度の
リード文のみ。RSS body を信用せず本文は detail HTML 抽出に委譲。
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


class METISource:
    """METI 用 ``XxxSource`` (Pattern H、body 不信用)。"""

    name: ClassVar[SourceName] = SourceName("METI")
    endpoint_url: ClassVar[str] = "https://www.meti.go.jp/ml_index_release_atom.xml"
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
