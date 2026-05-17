"""Hugging Face Blog 用 Fetcher。

per-source 設計: HF 公式 blog は ``<description>`` を空文字で出力する。
RSS body を信用せず本文は後段 HTML 抽出 (trafilatura) に委ねる。
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from app.collection.domain.observed_article import ObservedOrigin
from app.collection.domain.source_completion_profile import DEFAULT_PROFILE
from app.collection.fetchers.tools.fetched_article import FetchedArticle
from app.collection.fetchers.tools.rss_parser import RssParser


class HuggingFaceBlogAdapter:
    """Hugging Face Blog 用 SourceAdapter (Pattern H、body 不信用)。"""

    NAME = "Hugging Face"
    ENDPOINT_URL = "https://huggingface.co/blog/feed.xml"
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
