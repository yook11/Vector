"""``Fetcher`` Protocol — per-source 取得実装の構造的契約。

各 Fetcher は 1 source 分の取得結果を
``AsyncIterator[AnalyzableArticle | ObservedArticle | ConversionRejection]``
で逐次 yield する。per-entry の変換不能は raise せず ``ConversionRejection``
値として stream に乗せる (async generator から per-entry raise すると source
stream 全体が止まるため)。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol

from app.collection.article_collection.fetched_article_converter import (
    ConversionRejection,
)
from app.collection.domain.analyzable_article import AnalyzableArticle
from app.collection.domain.observed_article import ObservedArticle


class Fetcher(Protocol):
    """1 source 分の取得を担う Fetcher の構造的契約。

    ``async def fetch(self, source_id: int) -> AsyncIterator[...]`` を満たせば
    よい (継承不要)。``NAME`` は ``FETCHERS`` dispatch キー
    (= ``news_sources.name``)、``ENDPOINT_URL`` はそのソースの feed/API
    endpoint。``source_id`` は永続化時の FK 値としてのみ使う。
    """

    NAME: str
    ENDPOINT_URL: str

    def fetch(
        self, source_id: int
    ) -> AsyncIterator[AnalyzableArticle | ObservedArticle | ConversionRejection]: ...
