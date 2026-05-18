"""``ArticleSource`` を駆動して passport を yield する薄い runner (P2-D)。

source 固有の取得 logic は ``XxxSource.collect(tools)`` に閉じ、
``ArticleFetcher`` は ``FetchTools`` (共通取得道具箱) を渡して Source を実行し、
yield される ``FetchedArticle`` を ``try_build_passport`` で
``AnalyzableArticle`` / ``ObservedArticle`` に変換するだけの薄い層。

``Fetcher`` Protocol (``protocol.py``) は ``NAME: str`` / ``ENDPOINT_URL: str``
で宣言され、本層が Source の identity を instance attr に格上げすることで
structural subtyping を満たす。per-source 知識は Source クラス属性
(``completion_profile`` / ``observed_origin`` / ``name``) を
``try_build_passport`` へ thread する。

``tools`` は省略時 fetch 毎に ``FetchTools()`` を新規構築する (旧
``adapter_factory`` の「fetch 毎に新 machinery」意味を保存)。test は
fixture 注入済 ``FetchTools`` を渡す。
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from app.collection.domain.analyzable_article import AnalyzableArticle
from app.collection.domain.observed_article import ObservedArticle
from app.collection.source_fetch.passport_builder import try_build_passport
from app.collection.source_fetch.tools.fetch_tools import FetchTools
from app.collection.sources.article_source import ArticleSource


class ArticleFetcher:
    """``ArticleSource`` を駆動して passport を yield する共通 Fetcher。

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
    ) -> AsyncIterator[AnalyzableArticle | ObservedArticle]:
        tools = self._tools if self._tools is not None else FetchTools()
        async for fetched in self._source.collect(tools):
            passport = try_build_passport(
                fetched,
                source_id=source_id,
                source_name=self._source.name,
                origin=self._source.observed_origin,
                profile=self._source.completion_profile,
            )
            if passport is not None:
                yield passport
