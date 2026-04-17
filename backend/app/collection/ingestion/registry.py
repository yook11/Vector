"""ソースレジストリ — SourceName からフェッチャーへのマッピング。

全ソースを登録し、ソース名に応じた SourceFetcher を返す。
各ソースは固有のフェッチャーインスタンスを持つ。
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.collection.ingestion.persister import SourceFetchResult
from app.domain.news_source import SourceName
from app.models.news_source import NewsSource


@runtime_checkable
class SourceFetcher(Protocol):
    """ソースフェッチャーの共通インターフェース。"""

    async def fetch(
        self,
        client: httpx.AsyncClient,
        session: AsyncSession,
        source: NewsSource,
    ) -> SourceFetchResult: ...


def _build_registry() -> dict[SourceName, SourceFetcher]:
    """全ソースのフェッチャーを登録する。"""
    from app.collection.ingestion.fetchers.alpha_vantage import AlphaVantageFetcher
    from app.collection.ingestion.fetchers.hacker_news import HackerNewsFetcher
    from app.collection.ingestion.fetchers.rss.biopharma_dive import BioPharmaFetcher
    from app.collection.ingestion.fetchers.rss.cointelegraph import (
        CointelegraphFetcher,
    )
    from app.collection.ingestion.fetchers.rss.fierce_biotech import (
        FierceBiotechFetcher,
    )
    from app.collection.ingestion.fetchers.rss.itmedia import ITmediaFetcher
    from app.collection.ingestion.fetchers.rss.quantum_insider import (
        QuantumInsiderFetcher,
    )
    from app.collection.ingestion.fetchers.rss.techcrunch import TechCrunchFetcher
    from app.collection.ingestion.fetchers.rss.yahoo_finance import (
        YahooFinanceFetcher,
    )

    return {
        # RSS ソース（ソースごとに個別フェッチャー）
        SourceName("TechCrunch"): TechCrunchFetcher(),
        SourceName("FierceBiotech"): FierceBiotechFetcher(),
        SourceName("BioPharma Dive"): BioPharmaFetcher(),
        SourceName("The Quantum Insider"): QuantumInsiderFetcher(),
        SourceName("Cointelegraph"): CointelegraphFetcher(),
        SourceName("Yahoo Finance"): YahooFinanceFetcher(),
        SourceName("ITmedia"): ITmediaFetcher(),
        # API ソース
        SourceName("Hacker News"): HackerNewsFetcher(),
        SourceName("Alpha Vantage"): AlphaVantageFetcher(),
    }


_REGISTRY: dict[SourceName, SourceFetcher] | None = None


def get_fetcher(source: NewsSource) -> SourceFetcher:
    """ソースに対応するフェッチャーを返す。

    未登録のソース名の場合は KeyError を送出する。

    Args:
        source: フェッチャーを取得する対象のソース。

    Returns:
        SourceFetcher の実装。

    Raises:
        KeyError: ソース名がレジストリに登録されていない場合。
    """
    global _REGISTRY  # noqa: PLW0603
    if _REGISTRY is None:
        _REGISTRY = _build_registry()
    return _REGISTRY[source.name]
