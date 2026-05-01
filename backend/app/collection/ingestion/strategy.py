"""新 Protocol Fetcher の per-source dispatch table。

collection-acquisition-redesign Phase 1 完結 (PR-1e merge 後)。全 19 ソース
(RSS 18 + API 1 = Hacker News) が ``Fetcher`` Protocol を満たす per-source
実装に収束し、本ファイルが唯一の dispatch エントリポイントとなる
(``ingest_source`` task で参照される)。

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
from app.collection.ingestion.fetchers.hacker_news import HackerNewsFetcher
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

FETCHERS: Final[dict[str, Callable[[], Fetcher]]] = {
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
    "Hacker News": HackerNewsFetcher,
}
