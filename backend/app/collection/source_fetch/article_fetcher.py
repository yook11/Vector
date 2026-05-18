"""``ArticleSource`` を駆動して獲得型 / 棄却を yield する薄い runner。

``XxxSource.collect(tools)`` を実行し、yield された ``FetchedArticle`` を
``convert_fetched_article`` で獲得型に変換する。変換不能 entry の
``FetchedArticleConversionError`` は stream 境界で ``ConversionRejection`` 値に
変換して yield する (per-entry raise は source stream 全体を止めるため)。
棄却の監査は下流 Service の責務 (本層は DB session を持たない)。``tools``
省略時は fetch 毎に ``FetchTools()`` を新規構築する。
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from app.collection.domain.analyzable_article import AnalyzableArticle
from app.collection.domain.observed_article import ObservedArticle
from app.collection.source_fetch.errors import FetchedArticleConversionError
from app.collection.source_fetch.fetched_article_converter import (
    ConversionRejection,
    convert_fetched_article,
)
from app.collection.source_fetch.tools.fetch_tools import FetchTools
from app.collection.sources.article_source import ArticleSource


class ArticleFetcher:
    """``ArticleSource`` を駆動して獲得型 / 棄却を yield する共通 Fetcher。

    ``Fetcher`` 契約互換のため Source の ``name`` / ``endpoint_url`` を
    instance attr に格上げする。
    """

    def __init__(self, source: ArticleSource, tools: FetchTools | None = None) -> None:
        self._source = source
        self._tools = tools
        self.NAME: str = str(source.name)
        self.ENDPOINT_URL: str = source.endpoint_url

    async def fetch(
        self, source_id: int
    ) -> AsyncIterator[AnalyzableArticle | ObservedArticle | ConversionRejection]:
        tools = self._tools if self._tools is not None else FetchTools()
        async for fetched in self._source.collect(tools):
            try:
                yield convert_fetched_article(
                    fetched, source=self._source, source_id=source_id
                )
            except FetchedArticleConversionError as exc:
                # per-entry の変換不能は stream を止めず値化して下流へ。
                # 監査 (別 tx 書込) は Service が担う。
                yield ConversionRejection(error=exc)
