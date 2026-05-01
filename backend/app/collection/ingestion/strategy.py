"""新ルート (collection-acquisition-redesign Phase 1) のソース戦略表。

Strangler 移行期間中の hardcode set + factory dict。Phase 1d 完了時点で
18/19 ソース移行済 (RSS 全 18 ソース完全移行、Hacker News のみ旧経路に残)。
HN を新 Protocol API Pattern で実装する PR-1e 完了時に本ファイルごと
削除し、新 Protocol を全ソースの唯一の取得経路に収束させる。

設計判断:

- env / Settings を読まず hardcode (Pure DI)
- 判定キーは ``news_sources.name`` (StrEnum 値) — id は環境差で揺れ得るため
- factory は ``Callable[[], Fetcher]`` — Fetcher は無状態のため毎回 new で OK
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Final

from app.collection.ingestion.fetchers.cleantechnica import CleanTechnicaFetcher
from app.collection.ingestion.fetchers.eetimes_japan import EETimesJapanFetcher
from app.collection.ingestion.fetchers.electrek import ElectrekFetcher
from app.collection.ingestion.fetchers.engadget import EngadgetFetcher
from app.collection.ingestion.fetchers.fierce_biotech import FierceBiotechFetcher
from app.collection.ingestion.fetchers.ieee_spectrum import IEEESpectrumFetcher
from app.collection.ingestion.fetchers.itmedia_ai import ITmediaAIFetcher
from app.collection.ingestion.fetchers.itmedia_news import ITmediaNewsFetcher
from app.collection.ingestion.fetchers.jpcert import JPCERTFetcher
from app.collection.ingestion.fetchers.krebs_on_security import KrebsOnSecurityFetcher
from app.collection.ingestion.fetchers.microsoft_research import (
    MicrosoftResearchFetcher,
)
from app.collection.ingestion.fetchers.monoist import MONOistFetcher
from app.collection.ingestion.fetchers.nasa import NASAFetcher
from app.collection.ingestion.fetchers.protocol import Fetcher
from app.collection.ingestion.fetchers.quantum_insider import QuantumInsiderFetcher
from app.collection.ingestion.fetchers.spaceflight_now import SpaceflightNowFetcher
from app.collection.ingestion.fetchers.spacenews import SpaceNewsFetcher
from app.collection.ingestion.fetchers.techcrunch import TechCrunchFetcher
from app.collection.ingestion.fetchers.the_register import TheRegisterFetcher
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
    "ITmedia AI+": ITmediaAIFetcher,
    "ITmedia NEWS": ITmediaNewsFetcher,
    "MONOist": MONOistFetcher,
    "EE Times Japan": EETimesJapanFetcher,
    "Engadget": EngadgetFetcher,
    "FierceBiotech": FierceBiotechFetcher,
    "JPCERT/CC": JPCERTFetcher,
    "CleanTechnica": CleanTechnicaFetcher,
    "Electrek": ElectrekFetcher,
    "SpaceNews": SpaceNewsFetcher,
    "The Register": TheRegisterFetcher,
}

NEW_ROUTE_SOURCE_NAMES: Final[frozenset[str]] = frozenset(NEW_ROUTE_FETCHERS.keys())
