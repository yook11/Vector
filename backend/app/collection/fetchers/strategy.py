"""Adapter 駆動の per-source dispatch table (composition root)。

collection-acquisition-redesign Phase 1 → fetcher big-bang リファクタ P6 完結。
全 45 ソースが ``SourceAdapter`` を ``ArticleFetcher`` で駆動する形に収束し、
本ファイルが唯一の dispatch エントリポイントとなる (``ingest_source`` task で
参照される)。

設計判断:

- env / Settings を読まず hardcode (Pure DI)
- 判定キーは ``news_sources.name`` (= 各 Adapter の ``NAME`` ClassVar) —
  id は環境差で揺れ得るため
- per-source 知識 (補完方針 / 取得出自) は各 ``SourceAdapter`` の
  ``completion_profile`` / ``observed_origin`` ClassVar が所有する。
  ``SOURCES`` は ``NAME → Adapter class`` の純レジストリで、Stage 2 の
  ``CompletionProfileResolver`` が **無 instantiation** で
  ``.completion_profile`` を引くために参照する
- ``FETCHERS`` は ``SOURCES`` から導出する (2 辞書並走の desync を構造排除)。
  ``ArticleFetcher`` は無状態のため factory は毎回 new で OK
  (``lambda A=A: ArticleFetcher(A())`` 形)。``ArticleFetcher`` は Adapter の
  ``NAME`` / ``ENDPOINT_URL`` を instance attr に格上げするため ``Fetcher``
  Protocol を構造的に満たす
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
from app.collection.fetchers.tools.fetched_article import SourceAdapter
from app.collection.fetchers.venturebeat import VentureBeatAdapter

# 1 ニュースソース = 1 ``SourceAdapter`` クラス。順序は旧 ``FETCHERS`` を踏襲
# (``FETCHERS`` の iteration order を byte 不変に保つ)。
_ADAPTERS: Final[tuple[type[SourceAdapter], ...]] = (
    VentureBeatAdapter,
    TechCrunchAdapter,
    QuantumInsiderAdapter,
    KrebsOnSecurityAdapter,
    SpaceflightNowAdapter,
    NASAAdapter,
    IEEESpectrumAdapter,
    MicrosoftResearchAdapter,
    ITmediaAIAdapter,
    ITmediaNewsAdapter,
    MONOistAdapter,
    EETimesJapanAdapter,
    EngadgetAdapter,
    FierceBiotechAdapter,
    JPCERTAdapter,
    CleanTechnicaAdapter,
    ElectrekAdapter,
    SpaceNewsAdapter,
    TheRegisterAdapter,
    HackerNewsAdapter,
    MEXTAdapter,
    MICAdapter,
    METIAdapter,
    AnthropicAdapter,
    NISTAdapter,
    NSFAdapter,
    CloudflareBlogAdapter,
    DeepMindAdapter,
    ESAHubbleAdapter,
    ESAWebbAdapter,
    OpenAIAdapter,
    HuggingFaceBlogAdapter,
    ELifeAdapter,
    PLOSOneAdapter,
    MetaAIAdapter,
    CornellChronicleAdapter,
    FrontiersAIAdapter,
    FrontiersRoboticsAIAdapter,
    FrontiersEnergyResearchAdapter,
    FrontiersMaterialsAdapter,
    ORNLAdapter,
    MDPIMaterialsAdapter,
    MDPIEnergiesAdapter,
    MDPISensorsAdapter,
    MDPINanomaterialsAdapter,
)

# ``NAME → Adapter class`` の純レジストリ。Stage 2 resolver はここから
# 無 instantiation で ``.completion_profile`` / ``.observed_origin`` を引く。
SOURCES: Final[dict[str, type[SourceAdapter]]] = {A.NAME: A for A in _ADAPTERS}

# ``ingest_source`` task の ``FETCHERS[arg.name]`` 消費は無改修。``SOURCES``
# から導出することで「name→adapter」と「name→fetcher」の desync を構造排除。
FETCHERS: Final[dict[str, Callable[[], Fetcher]]] = {
    name: (lambda A=A: ArticleFetcher(A())) for name, A in SOURCES.items()
}
