"""``ArticleSource`` レジストリ (composition root)。

``acquire_source`` task が参照する唯一の dispatch エントリポイント。env を
読まず hardcode (Pure DI)、判定キーは ``news_sources.name``。
"""

from __future__ import annotations

from typing import Final

from app.collection.sources.article_source import ArticleSource
from app.collection.sources.definitions.anthropic import AnthropicSource
from app.collection.sources.definitions.cleantechnica import CleanTechnicaSource
from app.collection.sources.definitions.cloudflare import CloudflareBlogSource
from app.collection.sources.definitions.cornell import CornellChronicleSource
from app.collection.sources.definitions.deepmind import DeepMindSource
from app.collection.sources.definitions.eetimes_japan import EETimesJapanSource
from app.collection.sources.definitions.electrek import ElectrekSource
from app.collection.sources.definitions.elife import ELifeSource
from app.collection.sources.definitions.engadget import EngadgetSource
from app.collection.sources.definitions.esa import (
    ESAHubbleSource,
    ESAWebbSource,
)
from app.collection.sources.definitions.fierce_biotech import FierceBiotechSource
from app.collection.sources.definitions.frontiers import (
    FrontiersAISource,
    FrontiersEnergyResearchSource,
    FrontiersMaterialsSource,
    FrontiersRoboticsAISource,
)
from app.collection.sources.definitions.hacker_news import HackerNewsSource
from app.collection.sources.definitions.huggingface import HuggingFaceBlogSource
from app.collection.sources.definitions.ieee_spectrum import IEEESpectrumSource
from app.collection.sources.definitions.itmedia_ai import ITmediaAISource
from app.collection.sources.definitions.itmedia_news import ITmediaNewsSource
from app.collection.sources.definitions.jpcert import JPCERTSource
from app.collection.sources.definitions.krebs_on_security import KrebsOnSecuritySource
from app.collection.sources.definitions.mdpi import (
    MDPIEnergiesSource,
    MDPIMaterialsSource,
    MDPINanomaterialsSource,
    MDPISensorsSource,
)
from app.collection.sources.definitions.meta_ai import MetaAISource
from app.collection.sources.definitions.meti import METISource
from app.collection.sources.definitions.mext import MEXTSource
from app.collection.sources.definitions.mic import MICSource
from app.collection.sources.definitions.microsoft_research import (
    MicrosoftResearchSource,
)
from app.collection.sources.definitions.monoist import MONOistSource
from app.collection.sources.definitions.nasa import NASASource
from app.collection.sources.definitions.nist import NISTSource
from app.collection.sources.definitions.nsf import NSFSource
from app.collection.sources.definitions.openai import OpenAISource
from app.collection.sources.definitions.ornl import ORNLSource
from app.collection.sources.definitions.plos_one import PLOSOneSource
from app.collection.sources.definitions.quantum_insider import QuantumInsiderSource
from app.collection.sources.definitions.spaceflight_now import SpaceflightNowSource
from app.collection.sources.definitions.spacenews import SpaceNewsSource
from app.collection.sources.definitions.techcrunch import TechCrunchSource
from app.collection.sources.definitions.the_register import TheRegisterSource
from app.collection.sources.definitions.venturebeat import VentureBeatSource
from app.shared.value_objects.source_name import SourceName

# 順序は既存登録順を踏襲 (登録順の安定性を保つ)。
_SOURCES_LIST: Final[tuple[ArticleSource, ...]] = (
    VentureBeatSource,
    TechCrunchSource,
    QuantumInsiderSource,
    KrebsOnSecuritySource,
    SpaceflightNowSource,
    NASASource,
    IEEESpectrumSource,
    MicrosoftResearchSource,
    ITmediaAISource,
    ITmediaNewsSource,
    MONOistSource,
    EETimesJapanSource,
    EngadgetSource,
    FierceBiotechSource,
    JPCERTSource,
    CleanTechnicaSource,
    ElectrekSource,
    SpaceNewsSource,
    TheRegisterSource,
    HackerNewsSource,
    MEXTSource,
    MICSource,
    METISource,
    AnthropicSource,
    NISTSource,
    NSFSource,
    CloudflareBlogSource,
    DeepMindSource,
    ESAHubbleSource,
    ESAWebbSource,
    OpenAISource,
    HuggingFaceBlogSource,
    ELifeSource,
    PLOSOneSource,
    MetaAISource,
    CornellChronicleSource,
    FrontiersAISource,
    FrontiersRoboticsAISource,
    FrontiersEnergyResearchSource,
    FrontiersMaterialsSource,
    ORNLSource,
    MDPIMaterialsSource,
    MDPIEnergiesSource,
    MDPISensorsSource,
    MDPINanomaterialsSource,
)

# ``SourceName → ArticleSource`` レジストリ。
SOURCES: Final[dict[SourceName, ArticleSource]] = {
    source.name: source for source in _SOURCES_LIST
}
