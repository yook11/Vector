"""``ArticleSource`` 集約を駆動する共通 Fetcher (P2)。

source 固有の取得 logic は ``ArticleSource`` が factory 経由で持つ
``SourceAdapter`` machinery (``collect()``) に閉じ、``ArticleFetcher`` は
machinery が yield する ``FetchedArticle`` を ``try_build_passport`` で
``AnalyzableArticle`` / ``ObservedArticle`` に変換するだけの薄い層。

``Fetcher`` Protocol (``protocol.py``) は ``NAME: str`` / ``ENDPOINT_URL: str``
で宣言され、本層が ``ArticleSource`` の identity を instance attr に格上げ
することで structural subtyping を満たす (runtime / type checker いずれでも
問題なし)。

per-source 知識は ``ArticleSource`` フィールドを ``try_build_passport`` へ
thread する: ``completion_profile`` (補完方針)、``observed_origin`` (取得
チャネル / audit)、``name`` (観測事実の出所 = ``SourceName``)。P1 までは
コンストラクタが ``adapter`` 1 引数だったが、P2 で「Source 中心」へ転換し
``ArticleSource`` 1 引数を受ける (取得 machinery は ``source.make_adapter()``
で **毎 fetch 構築** = 旧 ``lambda A=A: ArticleFetcher(A())`` の「fetch 毎に
新 instance」意味を保存)。
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from app.collection.domain.analyzable_article import AnalyzableArticle
from app.collection.domain.observed_article import ObservedArticle
from app.collection.fetchers.tools.passport_builder import try_build_passport
from app.collection.sources.article_source import ArticleSource


class ArticleFetcher:
    """``ArticleSource`` を駆動して passport を yield する共通 Fetcher。

    ``Fetcher`` Protocol との互換のため、Source の ``name`` / ``endpoint_url``
    を instance attr に格上げする (consumer は class attr / instance attr の
    どちらからでも読める)。補完方針 / 取得チャネル / 出所は
    ``source.completion_profile`` / ``source.observed_origin`` /
    ``source.name`` (per-source 知識) を ``try_build_passport`` へ伝播する。
    """

    def __init__(self, source: ArticleSource) -> None:
        self._source = source
        self.NAME: str = str(source.name)
        self.ENDPOINT_URL: str = source.endpoint_url

    async def fetch(
        self, source_id: int
    ) -> AsyncIterator[AnalyzableArticle | ObservedArticle]:
        adapter = self._source.make_adapter()
        async for fetched in adapter.collect():
            passport = try_build_passport(
                fetched,
                source_id=source_id,
                source_name=self._source.name,
                origin=self._source.observed_origin,
                profile=self._source.completion_profile,
            )
            if passport is not None:
                yield passport
