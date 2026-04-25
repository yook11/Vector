"""ソースレジストリ — SourceName からフェッチャーへのマッピング。

全ソースを登録し、ソース名に応じた SourceFetcher を返す。
各ソースは固有のフェッチャーインスタンスを持つ。
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

import httpx

from app.collection.domain.value_objects.source import SourceName
from app.collection.ingestion.domain import ArticleCandidate
from app.models.news_source import NewsSource
from app.shared.value_objects.safe_url import SafeUrl


@runtime_checkable
class SourceFetcher(Protocol):
    """ソースフェッチャーの共通インターフェース。

    Fetcher は外部配信形式を ``ArticleCandidate`` に正規化して返すだけで、
    DB 永続化は ``SourceFetchService`` 側の責務とする。
    """

    async def fetch(
        self,
        client: httpx.AsyncClient,
        source: NewsSource,
    ) -> dict[SafeUrl, ArticleCandidate]: ...


def _build_registry() -> dict[SourceName, SourceFetcher]:
    """全ソースのフェッチャーを登録する。"""
    from app.collection.ingestion.fetchers.hacker_news import HackerNewsFetcher
    from app.collection.ingestion.fetchers.rss.fierce_biotech import (
        FierceBiotechFetcher,
    )
    from app.collection.ingestion.fetchers.rss.ieee_spectrum import (
        IEEESpectrumFetcher,
    )
    from app.collection.ingestion.fetchers.rss.itmedia import ITmediaFetcher
    from app.collection.ingestion.fetchers.rss.jpcert import JPCERTFetcher
    from app.collection.ingestion.fetchers.rss.krebs_on_security import (
        KrebsOnSecurityFetcher,
    )
    from app.collection.ingestion.fetchers.rss.microsoft_research import (
        MicrosoftResearchFetcher,
    )
    from app.collection.ingestion.fetchers.rss.nasa import NASAFetcher
    from app.collection.ingestion.fetchers.rss.quantum_insider import (
        QuantumInsiderFetcher,
    )
    from app.collection.ingestion.fetchers.rss.spaceflight_now import (
        SpaceflightNowFetcher,
    )
    from app.collection.ingestion.fetchers.rss.techcrunch import TechCrunchFetcher
    from app.collection.ingestion.fetchers.rss.venturebeat import VentureBeatFetcher

    return {
        # RSS ソース（ソースごとに個別フェッチャー）
        SourceName("TechCrunch"): TechCrunchFetcher(),
        SourceName("FierceBiotech"): FierceBiotechFetcher(),
        SourceName("The Quantum Insider"): QuantumInsiderFetcher(),
        SourceName("IEEE Spectrum"): IEEESpectrumFetcher(),
        SourceName("NASA"): NASAFetcher(),
        SourceName("Microsoft Research"): MicrosoftResearchFetcher(),
        SourceName("Krebs on Security"): KrebsOnSecurityFetcher(),
        SourceName("VentureBeat"): VentureBeatFetcher(),
        SourceName("Spaceflight Now"): SpaceflightNowFetcher(),
        SourceName("ITmedia AI+"): ITmediaFetcher(),
        SourceName("JPCERT/CC"): JPCERTFetcher(),
        # API ソース
        SourceName("Hacker News"): HackerNewsFetcher(),
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
