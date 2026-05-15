"""Adapter 駆動の共通 Fetcher 実装。

source 固有の取得 logic は ``SourceAdapter.collect()`` に閉じ、
``ArticleFetcher`` は Adapter が yield する ``FetchedArticle`` を
``try_build_passport_from_fetched`` で ``ReadyForArticle`` /
``IncompleteArticle`` に変換するだけの薄い層。

``Fetcher`` Protocol (``protocol.py``) は ``NAME: ClassVar[str]`` で宣言されて
いるが、Protocol の structural subtyping は instance attr でも満たされる
(runtime / type checker いずれでも問題なし)。後続 PR (``strategy.py`` 切替
タイミング) で ``ClassVar[str]`` → ``str`` への緩和を検討する。
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from app.collection.article.domain.article import ReadyForArticle
from app.collection.fetchers.tools.fetched_article import SourceAdapter
from app.collection.fetchers.tools.passport_builder import (
    try_build_passport_from_fetched,
)
from app.collection.incomplete_article.domain.incomplete_article import (
    IncompleteArticle,
)


class ArticleFetcher:
    """``SourceAdapter`` を駆動して passport を yield する共通 Fetcher。

    ``Fetcher`` Protocol との互換のため、Adapter の ``NAME`` / ``ENDPOINT_URL``
    を instance attr に格上げする (consumer は class attr / instance attr の
    どちらからでも読める)。
    """

    def __init__(self, adapter: SourceAdapter) -> None:
        self._adapter = adapter
        self.NAME: str = adapter.NAME
        self.ENDPOINT_URL: str = adapter.ENDPOINT_URL

    async def fetch(
        self, source_id: int
    ) -> AsyncIterator[ReadyForArticle | IncompleteArticle]:
        async for fetched in self._adapter.collect():
            passport = try_build_passport_from_fetched(fetched, source_id=source_id)
            if passport is not None:
                yield passport
