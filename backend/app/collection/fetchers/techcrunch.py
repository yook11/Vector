"""TechCrunch 用 Fetcher — Pattern H (RSS で URL 列挙、本文は HTML 必須) の参照実装。

per-source 設計: TC の RSS feed は ``<description>`` にリード文 (~140 chars)
しか含まず ``<content:encoded>`` も提供しない (`spec
collection-source-rss-research.md`)。Fetcher は **本文を取りに行かない** —
URL + title を ``IncompleteArticle`` として yield し、後段の
``ArticleCompletionService`` が HTML 本文を取得 + promotion する 2 段構成。

HTTP 取得 / feedparser / SSRF guard / title plain text 正規化は L2
``RssParser`` に集約済。本ファイルは L3 翻訳 (RssEntry → IncompleteArticle)
の per-source 責務だけを持つ。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import ClassVar

from app.collection.article.domain.value_objects import PublishedAt
from app.collection.fetchers.tools.rss_parser import RssEntry, RssParser
from app.collection.incomplete_article.domain.incomplete_article import (
    IncompleteArticle,
)
from app.shared.value_objects.canonical_article_url import CanonicalArticleUrl


class TechCrunchFetcher:
    """TechCrunch 用 Pattern H Fetcher。"""

    NAME: ClassVar[str] = "TechCrunch"
    ENDPOINT_URL: ClassVar[str] = "https://techcrunch.com/feed/"

    def __init__(self, parser: RssParser | None = None) -> None:
        self._parser = parser or RssParser()

    async def fetch(self, source_id: int) -> AsyncIterator[IncompleteArticle]:
        entries = await self._parser.fetch(
            endpoint_url=self.ENDPOINT_URL,
            source_name=self.NAME,
            parse_mode="text",
        )
        for entry in entries:
            item = self._convert_entry(entry, source_id)
            if item is not None:
                yield item

    def _convert_entry(
        self,
        entry: RssEntry,
        source_id: int,
    ) -> IncompleteArticle | None:
        """1 entry を ``IncompleteArticle`` に変換する。

        Pattern H 固有の品質ゲート (Pattern R より緩い):

        - ``title`` 空 → drop
        - ``link`` 不正 → drop
        - ``published_at`` 欠落 → drop しない (HTML 補完を待つ)
        - ``body`` は本実装では検査しない (Stage 2 の責務)
        """
        title = entry.title[:500]
        if not title:
            return None

        try:
            source_url = CanonicalArticleUrl(entry.link)
        except ValueError:
            return None

        published_at_hint = (
            PublishedAt(value=entry.published) if entry.published else None
        )
        return IncompleteArticle(
            title=title,
            source_id=source_id,
            source_url=source_url,
            published_at_hint=published_at_hint,
        )
