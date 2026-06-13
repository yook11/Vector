"""``XxxSource`` 経路の RSS 共通不変条件テスト (P2-D)。

取得 → 変換の本番経路 (``fetch_articles`` → ``fetched_article_converter``) を
通すとき、各 source が以下の不変条件を満たすことを fixture ベースで検証する:

- 実 fixture から少なくとも 1 件は永続化 passport を yield する
- yield された passport の型は ``allowed_types`` 集合に属する
- ``must_include_types`` の各型を最低 1 件含む (主経路の挙動を固定)
- yield された passport は永続化不変条件 (Stage 2 を通せば analyzable_articles に
  永続化できる) を満たす

取得 machinery は ``fetch_articles`` engine が ``XxxSource.read`` を駆動する形に
なった。本テストはネットワーク I/O を排除するため、``ReaderTools`` の ``rss`` を
``_FixtureRssReader`` に差し替える単一注入ヘルパ ``fixture_tools`` を使い、
Source クラスオブジェクトを ``drive_source`` (収集 → 変換) 本番経路に
通す。fixture / 期待型集合は P1 時点から不変 = yield される passport の型・
dedup・parse の同一性が byte 不変の証跡になる。

NASA / Cornell は ``multi_feed_rss`` free function へ、Frontiers×4 /
ESA×2 は ``frontiers_entries`` / ``djangoplicity_entries`` free function へ
``collect`` が委譲する。``_FixtureRssReader`` は endpoint_url / source_name を
無視して同 fixture を返すため、共有 machinery 経路でも旧 invariant と同一の
passport ストリームになる (NASA は multi-feed dedup を ``seen_urls`` が吸収)。
"""

from __future__ import annotations

import pytest

from app.collection.domain.analyzable_article import AnalyzableArticle
from app.collection.domain.observed_article import ObservedArticle
from app.collection.sources.article_source import ArticleSource
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
from app.collection.sources.definitions.huggingface import HuggingFaceBlogSource
from app.collection.sources.definitions.ieee_spectrum import IEEESpectrumSource
from app.collection.sources.definitions.itmedia_ai import ITmediaAISource
from app.collection.sources.definitions.itmedia_news import ITmediaNewsSource
from app.collection.sources.definitions.jpcert import JPCERTSource
from app.collection.sources.definitions.krebs_on_security import KrebsOnSecuritySource
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
from app.collection.sources.definitions.plos_one import PLOSOneSource
from app.collection.sources.definitions.quantum_insider import QuantumInsiderSource
from app.collection.sources.definitions.spaceflight_now import SpaceflightNowSource
from app.collection.sources.definitions.spacenews import SpaceNewsSource
from app.collection.sources.definitions.techcrunch import TechCrunchSource
from app.collection.sources.definitions.the_register import TheRegisterSource
from app.collection.sources.definitions.venturebeat import VentureBeatSource
from tests.collection.sources._fixture_tools import fixture_tools
from tests.collection.sources._invariant import (
    Passport,
    assert_at_least_one_passport,
    assert_passport_types_allowed,
    assert_passport_types_include,
    assert_passports_persistable,
    drive_source,
)

# 旧 invariant test と同じ Ready/Incomplete 集合表記。
_R_BODY_TRUSTED = {AnalyzableArticle, ObservedArticle}
_H_BODY_DISTRUSTED = {ObservedArticle}


# (label, SourceClass, fixture_filename, allowed_types, must_include_types)
_CASES: list[tuple[str, ArticleSource, str, set[type], set[type]]] = [
    (
        "VentureBeat-full",
        VentureBeatSource,
        "venturebeat_rss.xml",
        _R_BODY_TRUSTED,
        {AnalyzableArticle},
    ),
    (
        "VentureBeat-teaser",
        VentureBeatSource,
        "venturebeat_teaser_rss.xml",
        {ObservedArticle},
        {ObservedArticle},
    ),
    (
        "TechCrunch",
        TechCrunchSource,
        "techcrunch_rss.xml",
        _H_BODY_DISTRUSTED,
        {ObservedArticle},
    ),
    (
        "CleanTechnica",
        CleanTechnicaSource,
        "cleantechnica_rss.xml",
        _H_BODY_DISTRUSTED,
        {ObservedArticle},
    ),
    (
        "DeepMind",
        DeepMindSource,
        "deepmind_rss.xml",
        _H_BODY_DISTRUSTED,
        {ObservedArticle},
    ),
    (
        "EETimesJapan",
        EETimesJapanSource,
        "eetimes_japan_rss.xml",
        _H_BODY_DISTRUSTED,
        {ObservedArticle},
    ),
    (
        "Electrek",
        ElectrekSource,
        "electrek_rss.xml",
        _H_BODY_DISTRUSTED,
        {ObservedArticle},
    ),
    (
        "Engadget",
        EngadgetSource,
        "engadget_rss.xml",
        _H_BODY_DISTRUSTED,
        {ObservedArticle},
    ),
    (
        "HuggingFace",
        HuggingFaceBlogSource,
        "huggingface_blog_rss.xml",
        _H_BODY_DISTRUSTED,
        {ObservedArticle},
    ),
    (
        "ITmediaAI",
        ITmediaAISource,
        "itmedia_ai_rss.xml",
        _H_BODY_DISTRUSTED,
        {ObservedArticle},
    ),
    (
        "ITmediaNews",
        ITmediaNewsSource,
        "itmedia_news_rss.xml",
        _H_BODY_DISTRUSTED,
        {ObservedArticle},
    ),
    ("JPCERT", JPCERTSource, "jpcert_rss.xml", _H_BODY_DISTRUSTED, {ObservedArticle}),
    ("METI", METISource, "meti_atom.xml", _H_BODY_DISTRUSTED, {ObservedArticle}),
    ("MEXT", MEXTSource, "mext_rdf.xml", _H_BODY_DISTRUSTED, {ObservedArticle}),
    ("MIC", MICSource, "mic_rdf.xml", _H_BODY_DISTRUSTED, {ObservedArticle}),
    (
        "MONOist",
        MONOistSource,
        "monoist_rss.xml",
        _H_BODY_DISTRUSTED,
        {ObservedArticle},
    ),
    ("NIST", NISTSource, "nist_rss.xml", _H_BODY_DISTRUSTED, {ObservedArticle}),
    ("NSF", NSFSource, "nsf_rss.xml", _H_BODY_DISTRUSTED, {ObservedArticle}),
    ("OpenAI", OpenAISource, "openai_rss.xml", _H_BODY_DISTRUSTED, {ObservedArticle}),
    (
        "SpaceNews",
        SpaceNewsSource,
        "spacenews_rss.xml",
        _H_BODY_DISTRUSTED,
        {ObservedArticle},
    ),
    (
        "CloudflareBlog",
        CloudflareBlogSource,
        "cloudflare_rss.xml",
        _R_BODY_TRUSTED,
        {AnalyzableArticle},
    ),
    ("ELife", ELifeSource, "elife_rss.xml", _R_BODY_TRUSTED, {AnalyzableArticle}),
    (
        "IEEESpectrum",
        IEEESpectrumSource,
        "ieee_spectrum_rss.xml",
        _R_BODY_TRUSTED,
        {AnalyzableArticle},
    ),
    (
        "PLOSOne",
        PLOSOneSource,
        "plos_one_atom.xml",
        _R_BODY_TRUSTED,
        {AnalyzableArticle},
    ),
    (
        "QuantumInsider",
        QuantumInsiderSource,
        "quantum_insider_rss.xml",
        _R_BODY_TRUSTED,
        {AnalyzableArticle},
    ),
    (
        "KrebsOnSecurity",
        KrebsOnSecuritySource,
        "krebs_on_security_rss.xml",
        _R_BODY_TRUSTED,
        {AnalyzableArticle},
    ),
    (
        "SpaceflightNow",
        SpaceflightNowSource,
        "spaceflight_now_rss.xml",
        _R_BODY_TRUSTED,
        {AnalyzableArticle},
    ),
    ("NASA", NASASource, "nasa_rss.xml", _R_BODY_TRUSTED, {AnalyzableArticle}),
    ("MetaAI", MetaAISource, "meta_ai_rss.xml", _R_BODY_TRUSTED, {AnalyzableArticle}),
    (
        "MicrosoftResearch",
        MicrosoftResearchSource,
        "microsoft_research_rss.xml",
        _R_BODY_TRUSTED,
        {AnalyzableArticle},
    ),
    (
        "FrontiersAI",
        FrontiersAISource,
        "frontiers_ai_rss.xml",
        _R_BODY_TRUSTED,
        {AnalyzableArticle},
    ),
    (
        "FrontiersRoboticsAI",
        FrontiersRoboticsAISource,
        "frontiers_robotics_ai_rss.xml",
        _R_BODY_TRUSTED,
        {AnalyzableArticle},
    ),
    (
        "FrontiersEnergyResearch",
        FrontiersEnergyResearchSource,
        "frontiers_energy_research_rss.xml",
        _R_BODY_TRUSTED,
        {AnalyzableArticle},
    ),
    (
        "FrontiersMaterials",
        FrontiersMaterialsSource,
        "frontiers_materials_rss.xml",
        _R_BODY_TRUSTED,
        {AnalyzableArticle},
    ),
    (
        "CornellChronicle",
        CornellChronicleSource,
        "cornell_rss.xml",
        _H_BODY_DISTRUSTED,
        {ObservedArticle},
    ),
    (
        "TheRegister",
        TheRegisterSource,
        "the_register_atom.xml",
        _H_BODY_DISTRUSTED,
        {ObservedArticle},
    ),
    (
        "FierceBiotech",
        FierceBiotechSource,
        "fierce_biotech_rss.xml",
        _H_BODY_DISTRUSTED,
        {ObservedArticle},
    ),
    (
        "ESAHubble",
        ESAHubbleSource,
        "esa_hubble_rss.xml",
        _H_BODY_DISTRUSTED,
        {ObservedArticle},
    ),
    (
        "ESAWebb",
        ESAWebbSource,
        "esa_webb_rss.xml",
        _H_BODY_DISTRUSTED,
        {ObservedArticle},
    ),
]


async def _collect_passports(
    source: ArticleSource, fixture_filename: str
) -> list[Passport]:
    """収集 → 変換の本番経路で fixture を流し passport を集める。

    ``ReaderTools`` の ``rss`` を fixture parser に差し替えて Source クラス
    オブジェクトを本番経路 (fetched_article_converter) に通す。profile / origin
    は Source クラスの ``ClassVar`` を直読みする (旧 synthetic ``ArticleSource``
    ラップを廃止、RSS 群は全て feed + DEFAULT_POLICY)。
    """
    return await drive_source(source, tools=fixture_tools(rss_fixture=fixture_filename))


@pytest.fixture(params=_CASES, ids=lambda c: f"{c[0]}-{c[2]}")
async def case(
    request: pytest.FixtureRequest,
) -> tuple[list[Passport], set[type], set[type]]:
    _label, source, fixture_name, allowed, must_include = request.param
    passports = await _collect_passports(source, fixture_name)
    return passports, allowed, must_include


async def test_fixture_yields_at_least_one_passport(
    case: tuple[list[Passport], set[type], set[type]],
) -> None:
    passports, _, _ = case
    assert_at_least_one_passport(passports)


async def test_passport_types_within_allowed_set(
    case: tuple[list[Passport], set[type], set[type]],
) -> None:
    passports, allowed, _ = case
    assert_passport_types_allowed(passports, allowed=allowed)


async def test_passport_main_route_types_present(
    case: tuple[list[Passport], set[type], set[type]],
) -> None:
    passports, _, must_include = case
    assert_passport_types_include(passports, must_include=must_include)


async def test_passports_satisfy_persistence_invariants(
    case: tuple[list[Passport], set[type], set[type]],
) -> None:
    passports, _, _ = case
    assert_passports_persistable(passports)
