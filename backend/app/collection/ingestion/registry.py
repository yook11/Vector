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
    from app.collection.ingestion.fetchers.rss.cleantechnica import (
        CleanTechnicaFetcher,
    )
    from app.collection.ingestion.fetchers.rss.eetimes_japan import (
        EETimesJapanFetcher,
    )
    from app.collection.ingestion.fetchers.rss.electrek import ElectrekFetcher
    from app.collection.ingestion.fetchers.rss.engadget import EngadgetFetcher
    from app.collection.ingestion.fetchers.rss.fierce_biotech import (
        FierceBiotechFetcher,
    )
    from app.collection.ingestion.fetchers.rss.itmedia import ITmediaFetcher
    from app.collection.ingestion.fetchers.rss.itmedia_news import (
        ITmediaNewsFetcher,
    )
    from app.collection.ingestion.fetchers.rss.jpcert import JPCERTFetcher
    from app.collection.ingestion.fetchers.rss.monoist import MONOistFetcher
    from app.collection.ingestion.fetchers.rss.spacenews import SpaceNewsFetcher
    from app.collection.ingestion.fetchers.rss.the_register import TheRegisterFetcher

    return {
        # RSS ソース（ソースごとに個別フェッチャー）
        # NOTE: VentureBeat / TechCrunch / The Quantum Insider / Krebs on
        # Security / Spaceflight Now / NASA / IEEE Spectrum / Microsoft
        # Research は collection-acquisition-redesign Phase 1a'/1b'/1c-A1/
        # 1c-A2 で新 Protocol Fetcher に移行済み (Pattern R 全 8 ソース完了)。
        # Strangler 移行期間中は ``strategy.NEW_ROUTE_FETCHERS`` 経由で
        # 取り込まれる。
        SourceName("FierceBiotech"): FierceBiotechFetcher(),
        SourceName("ITmedia AI+"): ITmediaFetcher(),
        SourceName("JPCERT/CC"): JPCERTFetcher(),
        SourceName("Engadget"): EngadgetFetcher(),
        SourceName("CleanTechnica"): CleanTechnicaFetcher(),
        SourceName("Electrek"): ElectrekFetcher(),
        SourceName("SpaceNews"): SpaceNewsFetcher(),
        SourceName("The Register"): TheRegisterFetcher(),
        SourceName("MONOist"): MONOistFetcher(),
        SourceName("EE Times Japan"): EETimesJapanFetcher(),
        SourceName("ITmedia NEWS"): ITmediaNewsFetcher(),
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
