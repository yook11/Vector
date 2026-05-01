"""新ルート (collection-acquisition-redesign Phase 1) のソース戦略表。

Strangler 移行期間中の hardcode set + factory dict。Phase 1c-C 完了時に
本ファイルごと削除し、新 Protocol を全ソースの唯一の取得経路に収束させる。

設計判断:

- env / Settings を読まず hardcode (Pure DI)
- 判定キーは ``news_sources.name`` (StrEnum 値) — id は環境差で揺れ得るため
- factory は ``Callable[[], Fetcher]`` — Fetcher は無状態のため毎回 new で OK
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Final

from app.collection.ingestion.fetchers.ieee_spectrum import IEEESpectrumFetcher
from app.collection.ingestion.fetchers.krebs_on_security import KrebsOnSecurityFetcher
from app.collection.ingestion.fetchers.microsoft_research import (
    MicrosoftResearchFetcher,
)
from app.collection.ingestion.fetchers.nasa import NASAFetcher
from app.collection.ingestion.fetchers.protocol import Fetcher
from app.collection.ingestion.fetchers.quantum_insider import QuantumInsiderFetcher
from app.collection.ingestion.fetchers.spaceflight_now import SpaceflightNowFetcher
from app.collection.ingestion.fetchers.techcrunch import TechCrunchFetcher
from app.collection.ingestion.fetchers.venturebeat import VentureBeatFetcher

NEW_ROUTE_FETCHERS: Final[dict[str, Callable[[], Fetcher]]] = {
    "VentureBeat": VentureBeatFetcher,
    "TechCrunch": TechCrunchFetcher,
    "The Quantum Insider": QuantumInsiderFetcher,
    "Krebs on Security": KrebsOnSecurityFetcher,
    "Spaceflight Now": SpaceflightNowFetcher,
    "NASA": NASAFetcher,
    "IEEE Spectrum": IEEESpectrumFetcher,
    "Microsoft Research": MicrosoftResearchFetcher,
}

NEW_ROUTE_SOURCE_NAMES: Final[frozenset[str]] = frozenset(NEW_ROUTE_FETCHERS.keys())
