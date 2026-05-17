"""Adapter 駆動の共通 Fetcher 実装。

source 固有の取得 logic は ``SourceAdapter.collect()`` に閉じ、
``ArticleFetcher`` は Adapter が yield する ``FetchedArticle`` を
``try_build_passport`` で ``AnalyzableArticle`` /
``ObservedArticle`` に変換するだけの薄い層。

``Fetcher`` Protocol (``protocol.py``) は ``NAME: str`` / ``ENDPOINT_URL: str``
で宣言され、本層が Adapter の ClassVar を instance attr に格上げすることで
structural subtyping を満たす (runtime / type checker いずれでも問題なし)。

per-source 知識は Adapter ClassVar を ``try_build_passport`` へ thread する:
``completion_profile`` (補完方針)、``observed_origin`` (取得チャネル / audit)、
``NAME`` (観測事実の出所 = ``SourceName``)。Adapter は composition root
(``strategy.py``) で配線され、コンストラクタ契約は ``adapter`` 1 引数で不変
(既存テスト・直接構築箇所を無改修に保つ)。
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from app.collection.domain.analyzable_article import AnalyzableArticle
from app.collection.domain.observed_article import ObservedArticle
from app.collection.fetchers.tools.fetched_article import SourceAdapter
from app.collection.fetchers.tools.passport_builder import try_build_passport
from app.shared.value_objects.source_name import SourceName


class ArticleFetcher:
    """``SourceAdapter`` を駆動して passport を yield する共通 Fetcher。

    ``Fetcher`` Protocol との互換のため、Adapter の ``NAME`` / ``ENDPOINT_URL``
    を instance attr に格上げする (consumer は class attr / instance attr の
    どちらからでも読める)。補完方針 / 取得チャネル / 出所は
    ``adapter.completion_profile`` / ``adapter.observed_origin`` /
    ``adapter.NAME`` (per-source 知識) を ``try_build_passport`` へ伝播する。
    """

    def __init__(self, adapter: SourceAdapter) -> None:
        self._adapter = adapter
        self.NAME: str = adapter.NAME
        self.ENDPOINT_URL: str = adapter.ENDPOINT_URL

    async def fetch(
        self, source_id: int
    ) -> AsyncIterator[AnalyzableArticle | ObservedArticle]:
        source_name = SourceName(self._adapter.NAME)
        async for fetched in self._adapter.collect():
            passport = try_build_passport(
                fetched,
                source_id=source_id,
                source_name=source_name,
                origin=self._adapter.observed_origin,
                profile=self._adapter.completion_profile,
            )
            if passport is not None:
                yield passport
