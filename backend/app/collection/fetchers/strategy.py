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

from app.collection.fetchers.anthropic import AnthropicFetcher
from app.collection.fetchers.cleantechnica import CleanTechnicaFetcher
from app.collection.fetchers.cloudflare import CloudflareBlogFetcher
from app.collection.fetchers.cornell import CornellChronicleFetcher
from app.collection.fetchers.deepmind import DeepMindFetcher
from app.collection.fetchers.eetimes_japan import EETimesJapanFetcher
from app.collection.fetchers.electrek import ElectrekFetcher
from app.collection.fetchers.elife import ELifeFetcher
from app.collection.fetchers.engadget import EngadgetFetcher
from app.collection.fetchers.esa.hubble import ESAHubbleFetcher
from app.collection.fetchers.esa.webb import ESAWebbFetcher
from app.collection.fetchers.fierce_biotech import FierceBiotechFetcher
from app.collection.fetchers.frontiers.artificial_intelligence import (
    FrontiersAIFetcher,
)
from app.collection.fetchers.frontiers.energy_research import (
    FrontiersEnergyResearchFetcher,
)
from app.collection.fetchers.frontiers.materials import (
    FrontiersMaterialsFetcher,
)
from app.collection.fetchers.frontiers.robotics_and_ai import (
    FrontiersRoboticsAIFetcher,
)
from app.collection.fetchers.hacker_news import HackerNewsFetcher
from app.collection.fetchers.huggingface import HuggingFaceBlogFetcher
from app.collection.fetchers.ieee_spectrum import IEEESpectrumFetcher
from app.collection.fetchers.itmedia_ai import ITmediaAIFetcher
from app.collection.fetchers.itmedia_news import ITmediaNewsFetcher
from app.collection.fetchers.jpcert import JPCERTFetcher
from app.collection.fetchers.krebs_on_security import KrebsOnSecurityFetcher
from app.collection.fetchers.mdpi.energies import MDPIEnergiesFetcher
from app.collection.fetchers.mdpi.materials import MDPIMaterialsFetcher
from app.collection.fetchers.mdpi.nanomaterials import (
    MDPINanomaterialsFetcher,
)
from app.collection.fetchers.mdpi.sensors import MDPISensorsFetcher
from app.collection.fetchers.meta_ai import MetaAIFetcher
from app.collection.fetchers.meti import METIFetcher
from app.collection.fetchers.mext import MEXTFetcher
from app.collection.fetchers.mic import MICFetcher
from app.collection.fetchers.microsoft_research import (
    MicrosoftResearchFetcher,
)
from app.collection.fetchers.monoist import MONOistFetcher
from app.collection.fetchers.nasa import NASAFetcher
from app.collection.fetchers.nist import NISTFetcher
from app.collection.fetchers.nsf import NSFFetcher
from app.collection.fetchers.openai import OpenAIFetcher
from app.collection.fetchers.ornl import ORNLNewsFetcher
from app.collection.fetchers.plos_one import PLOSOneFetcher
from app.collection.fetchers.protocol import Fetcher
from app.collection.fetchers.quantum_insider import QuantumInsiderFetcher
from app.collection.fetchers.spaceflight_now import SpaceflightNowFetcher
from app.collection.fetchers.spacenews import SpaceNewsFetcher
from app.collection.fetchers.techcrunch import TechCrunchFetcher
from app.collection.fetchers.the_register import TheRegisterFetcher
from app.collection.fetchers.venturebeat import VentureBeatFetcher

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
    "MEXT": MEXTFetcher,
    "MIC": MICFetcher,
    "METI": METIFetcher,
    "Anthropic": AnthropicFetcher,
    "NIST": NISTFetcher,
    "NSF": NSFFetcher,
    "The Cloudflare Blog": CloudflareBlogFetcher,
    "Google DeepMind": DeepMindFetcher,
    "ESA/Hubble": ESAHubbleFetcher,
    "ESA/Webb": ESAWebbFetcher,
    "OpenAI": OpenAIFetcher,
    "Hugging Face": HuggingFaceBlogFetcher,
    "eLife": ELifeFetcher,
    "PLOS ONE": PLOSOneFetcher,
    "Meta AI": MetaAIFetcher,
    "Cornell Chronicle": CornellChronicleFetcher,
    "Frontiers in Artificial Intelligence": FrontiersAIFetcher,
    "Frontiers in Robotics and AI": FrontiersRoboticsAIFetcher,
    "Frontiers in Energy Research": FrontiersEnergyResearchFetcher,
    "Frontiers in Materials": FrontiersMaterialsFetcher,
    "ORNL": ORNLNewsFetcher,
    "MDPI Materials": MDPIMaterialsFetcher,
    "MDPI Energies": MDPIEnergiesFetcher,
    "MDPI Sensors": MDPISensorsFetcher,
    "MDPI Nanomaterials": MDPINanomaterialsFetcher,
}
