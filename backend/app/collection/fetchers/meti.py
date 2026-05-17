"""METI (経済産業省) 用 Fetcher (Atom 1.0、UTF-8)。

per-source 設計: Atom 1.0 ルート。``<summary>`` は 300-400 字程度の
リード文のみ。RSS body を信用せず本文は detail HTML 抽出に委譲。
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from app.collection.domain.observed_article import ObservedOrigin
from app.collection.domain.source_completion_profile import DEFAULT_PROFILE
from app.collection.fetchers.tools.fetched_article import FetchedArticle
from app.collection.fetchers.tools.rss_parser import RssParser


class METIAdapter:
    """METI 用 SourceAdapter (Pattern H、body 不信用)。"""

    NAME = "METI"
    ENDPOINT_URL = "https://www.meti.go.jp/ml_index_release_atom.xml"
    observed_origin = ObservedOrigin.feed
    completion_profile = DEFAULT_PROFILE

    def __init__(self, parser: RssParser | None = None) -> None:
        self._parser = parser or RssParser()

    async def collect(self) -> AsyncIterator[FetchedArticle]:
        entries = await self._parser.fetch(
            endpoint_url=self.ENDPOINT_URL,
            source_name=self.NAME,
            parse_mode="bytes",
        )
        for entry in entries:
            yield FetchedArticle(
                title=entry.title,
                url=entry.link,
                body=None,
                published_at=entry.published,
            )
