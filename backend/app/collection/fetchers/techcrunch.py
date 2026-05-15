"""TechCrunch 用 Fetcher。

per-source 設計: TC の RSS feed は ``<description>`` にリード文 (~140 chars)
しか含まず ``<content:encoded>`` も提供しない (`spec
collection-source-rss-research.md`)。Fetcher は **RSS 本文を信用しない** —
``body_candidate=None`` を builder に渡し、URL + title を ``IncompleteArticle``
として yield する。後段の ``ArticleCompletionService`` が HTML 本文を取得 +
promotion する 2 段構成。

将来 TC が ``<content:encoded>`` に full body を載せるようになった場合、
``_pick_body`` 相当を持たせて builder に body を渡せば自然に Ready 経路に
切り替わる。

HTTP 取得 / feedparser / SSRF guard / title plain text 正規化は L2
``RssParser`` に集約済。本ファイルは L3 翻訳 (RssEntry → passport) の
per-source 責務だけを持つ。
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from app.collection.fetchers.tools.fetched_article import FetchedArticle
from app.collection.fetchers.tools.rss_parser import RssParser


class TechCrunchAdapter:
    """TechCrunch 用 SourceAdapter (新経路、Adapter 駆動)。

    旧 ``TechCrunchFetcher`` と並存させ、Adapter 経路では ``body=None`` を
    ``FetchedArticle`` に焼き込む形で ``passport_builder`` 側の Incomplete 経路に
    固定する。将来 TC が ``<content:encoded>`` を提供するようになったら、
    ``_to_fetched`` 内で body 候補を組み立てる差分だけで Ready 経路に昇格できる。
    """

    NAME = "TechCrunch"
    ENDPOINT_URL = "https://techcrunch.com/feed/"

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
