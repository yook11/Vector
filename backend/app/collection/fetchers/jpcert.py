"""JPCERT/CC 用 Fetcher (RDF / RSS 1.0)。

per-source 設計: feed が RDF (RSS 1.0) ルート (``<rdf:RDF>``)。``<title>``
は多行 + インデント空白を含むため ``RssParser.normalize_entry`` の whitespace
正規化で吸収する。RSS body を信用せず本文は HTML 抽出に委譲。
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from app.collection.fetchers.tools.fetched_article import FetchedArticle
from app.collection.fetchers.tools.rss_parser import RssParser


class JPCERTAdapter:
    """JPCERT/CC 用 SourceAdapter (Pattern H、body 不信用)。"""

    NAME = "JPCERT/CC"
    ENDPOINT_URL = "https://www.jpcert.or.jp/rss/jpcert.rdf"

    def __init__(self, parser: RssParser | None = None) -> None:
        self._parser = parser or RssParser()

    async def collect(self) -> AsyncIterator[FetchedArticle]:
        entries = await self._parser.fetch(
            endpoint_url=self.ENDPOINT_URL,
            source_name=self.NAME,
            parse_mode="text",
        )
        for entry in entries:
            yield FetchedArticle(
                title=entry.title,
                url=entry.link,
                body=None,
                published_at=entry.published,
            )
