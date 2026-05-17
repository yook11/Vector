"""``ArticleSource`` レジストリ (composition root, P2-D)。

collection-acquisition-redesign Phase 1 → fetcher big-bang → P2 Source 集約化
→ P2-D Adapter 概念除去 完結。全 45 ソースが「1 つの ``XxxSource`` クラスが
(a) どう fetch するか=``collect(tools)``、(b) どう完成させるか=
``completion_profile`` を ``ClassVar`` 宣言する」形に収束し、本ファイルが唯一
の dispatch エントリポイントとなる (``ingest_source`` task で参照される)。

設計判断:

- env / Settings を読まず hardcode (Pure DI)
- 判定キーは ``news_sources.name`` (= ``XxxSource.name``) — id は環境差で
  揺れ得るため
- per-source 知識 (補完方針 / 取得出自 / identity / 取得手順) は各
  ``XxxSource`` クラスが ``ClassVar`` / ``collect`` で所有する。``SOURCES`` は
  ``SourceName → ArticleSource`` (= Source クラスオブジェクト) のレジストリで、
  Stage 2 の ``CompletionProfileResolver`` が **無 instantiation** で
  ``.completion_profile`` を引くために参照する (Source は class そのものが
  ``ArticleSource`` Protocol を満たすため、``adapter_factory`` / ``make_adapter``
  のような構築経路が存在せず、profile 読みで machinery を作る経路が構造的に
  不能 = class-ref 構造保証。spec §4.6 ガードレール)
- ``FETCHERS`` は ``SOURCES`` から導出する (2 辞書並走の desync を構造排除)。
  ``ingest_source`` task の ``FETCHERS[arg.name]`` 消費 (str キー) は無改修の
  ため ``str(source.name)`` をキーにする。``ArticleFetcher`` は無状態のため
  factory は毎回 new で OK (``lambda s=s: ArticleFetcher(s)`` 形、default 引数
  束縛で per-source 値を固定)。``ArticleFetcher`` は Source の ``name`` /
  ``endpoint_url`` を instance attr に格上げするため ``Fetcher`` Protocol を
  構造的に満たす
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Final

from app.collection.fetchers.anthropic import AnthropicSource
from app.collection.fetchers.article_fetcher import ArticleFetcher
from app.collection.fetchers.cleantechnica import CleanTechnicaSource
from app.collection.fetchers.cloudflare import CloudflareBlogSource
from app.collection.fetchers.cornell import CornellChronicleSource
from app.collection.fetchers.deepmind import DeepMindSource
from app.collection.fetchers.eetimes_japan import EETimesJapanSource
from app.collection.fetchers.electrek import ElectrekSource
from app.collection.fetchers.elife import ELifeSource
from app.collection.fetchers.engadget import EngadgetSource
from app.collection.fetchers.esa.sources import ESAHubbleSource, ESAWebbSource
from app.collection.fetchers.fierce_biotech import FierceBiotechSource
from app.collection.fetchers.frontiers.sources import (
    FrontiersAISource,
    FrontiersEnergyResearchSource,
    FrontiersMaterialsSource,
    FrontiersRoboticsAISource,
)
from app.collection.fetchers.hacker_news import HackerNewsSource
from app.collection.fetchers.huggingface import HuggingFaceBlogSource
from app.collection.fetchers.ieee_spectrum import IEEESpectrumSource
from app.collection.fetchers.itmedia_ai import ITmediaAISource
from app.collection.fetchers.itmedia_news import ITmediaNewsSource
from app.collection.fetchers.jpcert import JPCERTSource
from app.collection.fetchers.krebs_on_security import KrebsOnSecuritySource
from app.collection.fetchers.mdpi.sources import (
    MDPIEnergiesSource,
    MDPIMaterialsSource,
    MDPINanomaterialsSource,
    MDPISensorsSource,
)
from app.collection.fetchers.meta_ai import MetaAISource
from app.collection.fetchers.meti import METISource
from app.collection.fetchers.mext import MEXTSource
from app.collection.fetchers.mic import MICSource
from app.collection.fetchers.microsoft_research import MicrosoftResearchSource
from app.collection.fetchers.monoist import MONOistSource
from app.collection.fetchers.nasa import NASASource
from app.collection.fetchers.nist import NISTSource
from app.collection.fetchers.nsf import NSFSource
from app.collection.fetchers.openai import OpenAISource
from app.collection.fetchers.ornl import ORNLSource
from app.collection.fetchers.plos_one import PLOSOneSource
from app.collection.fetchers.protocol import Fetcher
from app.collection.fetchers.quantum_insider import QuantumInsiderSource
from app.collection.fetchers.spaceflight_now import SpaceflightNowSource
from app.collection.fetchers.spacenews import SpaceNewsSource
from app.collection.fetchers.techcrunch import TechCrunchSource
from app.collection.fetchers.the_register import TheRegisterSource
from app.collection.fetchers.venturebeat import VentureBeatSource
from app.collection.sources.article_source import ArticleSource
from app.shared.value_objects.source_name import SourceName

# 1 ニュースソース = 1 ``XxxSource`` クラスオブジェクト。順序は P1 時点の
# 登録順を踏襲 (``FETCHERS`` の iteration order を byte 不変に保つ)。値は
# クラスそのもの (各々 ``ArticleSource`` Protocol を構造的に満たす)。
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

# ``SourceName → ArticleSource`` (= Source クラスオブジェクト) レジストリ。
# Stage 2 resolver はここから無 instantiation で ``.completion_profile`` /
# ``.observed_origin`` を引く。
SOURCES: Final[dict[SourceName, ArticleSource]] = {
    source.name: source for source in _SOURCES_LIST
}

# ``ingest_source`` task の ``FETCHERS[arg.name]`` 消費 (str キー) は無改修。
# ``SOURCES`` から導出することで「name→source」と「name→fetcher」の desync を
# 構造排除。``lambda s=s:`` の default 引数束縛で per-source 値を固定する。
FETCHERS: Final[dict[str, Callable[[], Fetcher]]] = {
    str(source.name): (lambda s=source: ArticleFetcher(s)) for source in _SOURCES_LIST
}
