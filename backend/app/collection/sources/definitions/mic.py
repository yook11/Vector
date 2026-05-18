"""MIC (総務省) 用 Source (RDF / RSS 1.0、Shift_JIS)。

per-source 設計: feed が RDF (RSS 1.0) 宣言で ``<?xml encoding="Shift_JIS"?>``。
``parse_mode="bytes"`` を選ぶことで feedparser が XML 宣言から Shift_JIS を
sniff できる (``response.text`` 経由だと httpx の charset 推定で文字化けする
ため)。RSS body を信用せず本文は HTML 抽出に委譲。
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
from app.collection.source_fetch.tools.fetch_tools import FetchTools
from app.shared.value_objects.source_name import SourceName


class MICSource:
    """MIC 用 ``XxxSource`` (Pattern H、body 不信用、Shift_JIS feed)。

    ``parse_mode="bytes"`` で feedparser に encoding sniff を任せる。
    """

    name: ClassVar[SourceName] = SourceName("MIC")
    endpoint_url: ClassVar[str] = "https://www.soumu.go.jp/news.rdf"
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
