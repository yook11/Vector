"""``ArticleSource`` を駆動して獲得型 / 棄却を yield する薄い runner (P2-D)。

source 固有の取得 logic は ``XxxSource.collect(tools)`` に閉じ、
``ArticleFetcher`` は ``FetchTools`` (共通取得道具箱) を渡して Source を実行し、
yield される ``FetchedArticle`` を ``convert_fetched_article`` で
``AnalyzableArticle`` / ``ObservedArticle`` に変換するだけの薄い層。

``convert_fetched_article`` は変換不能 entry に対し
``FetchedArticleConversionError`` を raise する純粋関数。本層はその例外を
**stream 境界で捕捉して ``ConversionRejection`` 値に変換** して yield する
(async generator から per-entry raise すると source stream 全体が止まり、
恒久不良 entry なら source 全体が恒久停止するため)。棄却の監査 (別 tx 書込)
は下流 Service の責務で、本層は DB session を持たない。

``Fetcher`` Protocol (``protocol.py``) は ``NAME: str`` / ``ENDPOINT_URL: str``
で宣言され、本層が Source の identity を instance attr に格上げすることで
structural subtyping を満たす。per-source 知識は Source クラスオブジェクトを
``convert_fetched_article`` へ渡すことで thread する。

``tools`` は省略時 fetch 毎に ``FetchTools()`` を新規構築する (旧
``adapter_factory`` の「fetch 毎に新 machinery」意味を保存)。test は
fixture 注入済 ``FetchTools`` を渡す。
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

    ``source`` は Source クラスオブジェクト (``ArticleSource`` Protocol を満たす)。
    ``Fetcher`` Protocol との互換のため Source の ``name`` / ``endpoint_url``
    を instance attr に格上げする。
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
