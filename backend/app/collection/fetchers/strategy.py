"""Adapter 駆動の per-source dispatch table。

collection-acquisition-redesign Phase 1 → fetcher big-bang リファクタ P6 完結。
全 45 ソースが ``SourceAdapter`` を ``ArticleFetcher`` で駆動する形に収束し、
本ファイルが唯一の dispatch エントリポイントとなる (``ingest_source`` task で
参照される)。

設計判断:

- env / Settings を読まず hardcode (Pure DI)
- 判定キーは ``news_sources.name`` (StrEnum 値) — id は環境差で揺れ得るため
- factory は ``Callable[[], Fetcher]`` — ``ArticleFetcher`` は無状態のため毎回
  new で OK。``lambda: ArticleFetcher(XxxAdapter())`` 形で Adapter を注入する
  (``ArticleFetcher`` は Adapter の ``NAME`` / ``ENDPOINT_URL`` を instance
  attr に格上げするため ``Fetcher`` Protocol を構造的に満たす)
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Final

from app.collection.fetchers.anthropic import AnthropicAdapter
from app.collection.fetchers.article_fetcher import ArticleFetcher
from app.collection.fetchers.cleantechnica import CleanTechnicaAdapter
from app.collection.fetchers.cloudflare import CloudflareBlogAdapter
from app.collection.fetchers.cornell import CornellChronicleAdapter
from app.collection.fetchers.deepmind import DeepMindAdapter
from app.collection.fetchers.eetimes_japan import EETimesJapanAdapter
from app.collection.fetchers.electrek import ElectrekAdapter
from app.collection.fetchers.elife import ELifeAdapter
from app.collection.fetchers.engadget import EngadgetAdapter
from app.collection.fetchers.esa.hubble import ESAHubbleAdapter
from app.collection.fetchers.esa.webb import ESAWebbAdapter
from app.collection.fetchers.fierce_biotech import FierceBiotechAdapter
from app.collection.fetchers.frontiers.artificial_intelligence import (
    FrontiersAIAdapter,
)
from app.collection.fetchers.frontiers.energy_research import (
    FrontiersEnergyResearchAdapter,
)
from app.collection.fetchers.frontiers.materials import (
    FrontiersMaterialsAdapter,
)
from app.collection.fetchers.frontiers.robotics_and_ai import (
    FrontiersRoboticsAIAdapter,
)
from app.collection.fetchers.hacker_news import HackerNewsAdapter
from app.collection.fetchers.huggingface import HuggingFaceBlogAdapter
from app.collection.fetchers.ieee_spectrum import IEEESpectrumAdapter
from app.collection.fetchers.itmedia_ai import ITmediaAIAdapter
from app.collection.fetchers.itmedia_news import ITmediaNewsAdapter
from app.collection.fetchers.jpcert import JPCERTAdapter
from app.collection.fetchers.krebs_on_security import KrebsOnSecurityAdapter
from app.collection.fetchers.mdpi.energies import MDPIEnergiesAdapter
from app.collection.fetchers.mdpi.materials import MDPIMaterialsAdapter
from app.collection.fetchers.mdpi.nanomaterials import (
    MDPINanomaterialsAdapter,
)
from app.collection.fetchers.mdpi.sensors import MDPISensorsAdapter
from app.collection.fetchers.meta_ai import MetaAIAdapter
from app.collection.fetchers.meti import METIAdapter
from app.collection.fetchers.mext import MEXTAdapter
from app.collection.fetchers.mic import MICAdapter
from app.collection.fetchers.microsoft_research import (
    MicrosoftResearchAdapter,
)
from app.collection.fetchers.monoist import MONOistAdapter
from app.collection.fetchers.nasa import NASAAdapter
from app.collection.fetchers.nist import NISTAdapter
from app.collection.fetchers.nsf import NSFAdapter
from app.collection.fetchers.openai import OpenAIAdapter
from app.collection.fetchers.ornl import ORNLAdapter
from app.collection.fetchers.plos_one import PLOSOneAdapter
from app.collection.fetchers.protocol import Fetcher
from app.collection.fetchers.quantum_insider import QuantumInsiderAdapter
from app.collection.fetchers.spaceflight_now import SpaceflightNowAdapter
from app.collection.fetchers.spacenews import SpaceNewsAdapter
from app.collection.fetchers.techcrunch import TechCrunchAdapter
from app.collection.fetchers.the_register import TheRegisterAdapter
from app.collection.fetchers.venturebeat import VentureBeatAdapter

FETCHERS: Final[dict[str, Callable[[], Fetcher]]] = {
    "VentureBeat": lambda: ArticleFetcher(VentureBeatAdapter()),
    "TechCrunch": lambda: ArticleFetcher(TechCrunchAdapter()),
    "The Quantum Insider": lambda: ArticleFetcher(QuantumInsiderAdapter()),
    "Krebs on Security": lambda: ArticleFetcher(KrebsOnSecurityAdapter()),
    "Spaceflight Now": lambda: ArticleFetcher(SpaceflightNowAdapter()),
    "NASA": lambda: ArticleFetcher(NASAAdapter()),
    "IEEE Spectrum": lambda: ArticleFetcher(IEEESpectrumAdapter()),
    "Microsoft Research": lambda: ArticleFetcher(MicrosoftResearchAdapter()),
    "ITmedia AI+": lambda: ArticleFetcher(ITmediaAIAdapter()),
    "ITmedia NEWS": lambda: ArticleFetcher(ITmediaNewsAdapter()),
    "MONOist": lambda: ArticleFetcher(MONOistAdapter()),
    "EE Times Japan": lambda: ArticleFetcher(EETimesJapanAdapter()),
    "Engadget": lambda: ArticleFetcher(EngadgetAdapter()),
    "FierceBiotech": lambda: ArticleFetcher(FierceBiotechAdapter()),
    "JPCERT/CC": lambda: ArticleFetcher(JPCERTAdapter()),
    "CleanTechnica": lambda: ArticleFetcher(CleanTechnicaAdapter()),
    "Electrek": lambda: ArticleFetcher(ElectrekAdapter()),
    "SpaceNews": lambda: ArticleFetcher(SpaceNewsAdapter()),
    "The Register": lambda: ArticleFetcher(TheRegisterAdapter()),
    "Hacker News": lambda: ArticleFetcher(HackerNewsAdapter()),
    "MEXT": lambda: ArticleFetcher(MEXTAdapter()),
    "MIC": lambda: ArticleFetcher(MICAdapter()),
    "METI": lambda: ArticleFetcher(METIAdapter()),
    "Anthropic": lambda: ArticleFetcher(AnthropicAdapter()),
    "NIST": lambda: ArticleFetcher(NISTAdapter()),
    "NSF": lambda: ArticleFetcher(NSFAdapter()),
    "The Cloudflare Blog": lambda: ArticleFetcher(CloudflareBlogAdapter()),
    "Google DeepMind": lambda: ArticleFetcher(DeepMindAdapter()),
    "ESA/Hubble": lambda: ArticleFetcher(ESAHubbleAdapter()),
    "ESA/Webb": lambda: ArticleFetcher(ESAWebbAdapter()),
    "OpenAI": lambda: ArticleFetcher(OpenAIAdapter()),
    "Hugging Face": lambda: ArticleFetcher(HuggingFaceBlogAdapter()),
    "eLife": lambda: ArticleFetcher(ELifeAdapter()),
    "PLOS ONE": lambda: ArticleFetcher(PLOSOneAdapter()),
    "Meta AI": lambda: ArticleFetcher(MetaAIAdapter()),
    "Cornell Chronicle": lambda: ArticleFetcher(CornellChronicleAdapter()),
    "Frontiers in Artificial Intelligence": lambda: ArticleFetcher(
        FrontiersAIAdapter()
    ),
    "Frontiers in Robotics and AI": lambda: ArticleFetcher(
        FrontiersRoboticsAIAdapter()
    ),
    "Frontiers in Energy Research": lambda: ArticleFetcher(
        FrontiersEnergyResearchAdapter()
    ),
    "Frontiers in Materials": lambda: ArticleFetcher(FrontiersMaterialsAdapter()),
    "ORNL": lambda: ArticleFetcher(ORNLAdapter()),
    "MDPI Materials": lambda: ArticleFetcher(MDPIMaterialsAdapter()),
    "MDPI Energies": lambda: ArticleFetcher(MDPIEnergiesAdapter()),
    "MDPI Sensors": lambda: ArticleFetcher(MDPISensorsAdapter()),
    "MDPI Nanomaterials": lambda: ArticleFetcher(MDPINanomaterialsAdapter()),
}
