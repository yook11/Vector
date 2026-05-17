"""MIC (総務省) 用 Fetcher (RDF / RSS 1.0、Shift_JIS)。

per-source 設計: feed が RDF (RSS 1.0) 宣言で ``<?xml encoding="Shift_JIS"?>``。
``parse_mode="bytes"`` を選ぶことで feedparser が XML 宣言から Shift_JIS を
sniff できる (``response.text`` 経由だと httpx の charset 推定で文字化けする
ため)。RSS body を信用せず本文は HTML 抽出に委譲。
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from app.collection.fetchers.tools.fetched_article import FetchedArticle
from app.collection.fetchers.tools.rss_parser import RssParser


class MICAdapter:
    """MIC 用 SourceAdapter (Pattern H、body 不信用、Shift_JIS feed)。

    ``parse_mode="bytes"`` で feedparser に encoding sniff を任せる。
    """

    def __init__(
        self,
        *,
        endpoint_url: str,
        source_name: str,
        parser: RssParser | None = None,
    ) -> None:
        self._endpoint_url = endpoint_url
        self._source_name = source_name
        self._parser = parser or RssParser()

    async def collect(self) -> AsyncIterator[FetchedArticle]:
        entries = await self._parser.fetch(
            endpoint_url=self._endpoint_url,
            source_name=self._source_name,
            parse_mode="bytes",
        )
        for entry in entries:
            yield FetchedArticle(
                title=entry.title,
                url=entry.link,
                body=None,
                published_at=entry.published,
            )
