"""SourceAdapter machinery 経路の RSS 共通不変条件テスト (P2)。

``ArticleFetcher(source)`` 本番経路と同じ ``passport_builder`` を通すとき、
各 source が以下の不変条件を満たすことを fixture ベースで検証する:

- 実 fixture から少なくとも 1 件は永続化 passport を yield する
- yield された passport の型は ``allowed_types`` 集合に属する
- ``must_include_types`` の各型を最低 1 件含む (主経路の挙動を固定)
- yield された passport は永続化不変条件 (Stage 2 を通せば articles に
  永続化できる) を満たす

P2 で取得 machinery は ``ArticleSource`` の ``adapter_factory`` 経由になった。
本テストはネットワーク I/O を排除するため、各 machinery を ``parser=``
``_FixtureRssParser`` を注入して直構築する (``adapter_factory`` は実
``RssParser`` を組むため fixture では使えない)。fixture / 期待型集合は P1 時点
から不変 = yield される passport の型・dedup・parse の同一性が byte 不変の
証跡になる。
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from pathlib import Path

import feedparser
import pytest

from app.collection.domain.analyzable_article import AnalyzableArticle
from app.collection.domain.observed_article import ObservedArticle
from app.collection.fetchers.article_fetcher import ArticleFetcher
from app.collection.fetchers.cleantechnica import CleanTechnicaAdapter
from app.collection.fetchers.cloudflare import CloudflareBlogAdapter
from app.collection.fetchers.cornell import CORNELL_FEEDS
from app.collection.fetchers.deepmind import DeepMindAdapter
from app.collection.fetchers.eetimes_japan import EETimesJapanAdapter
from app.collection.fetchers.electrek import ElectrekAdapter
from app.collection.fetchers.elife import ELifeAdapter
from app.collection.fetchers.engadget import EngadgetAdapter
from app.collection.fetchers.esa._common import DjangoplicityAdapter
from app.collection.fetchers.fierce_biotech import FierceBiotechAdapter
from app.collection.fetchers.frontiers._common import FrontiersJournalAdapter
from app.collection.fetchers.huggingface import HuggingFaceBlogAdapter
from app.collection.fetchers.ieee_spectrum import IEEESpectrumAdapter
from app.collection.fetchers.itmedia_ai import ITmediaAIAdapter
from app.collection.fetchers.itmedia_news import ITmediaNewsAdapter
from app.collection.fetchers.jpcert import JPCERTAdapter
from app.collection.fetchers.krebs_on_security import KrebsOnSecurityAdapter
from app.collection.fetchers.meta_ai import MetaAIAdapter
from app.collection.fetchers.meti import METIAdapter
from app.collection.fetchers.mext import MEXTAdapter
from app.collection.fetchers.mic import MICAdapter
from app.collection.fetchers.microsoft_research import MicrosoftResearchAdapter
from app.collection.fetchers.monoist import MONOistAdapter
from app.collection.fetchers.nasa import NASA_FEEDS, nasa_build_body
from app.collection.fetchers.nist import NISTAdapter
from app.collection.fetchers.nsf import NSFAdapter
from app.collection.fetchers.openai import OpenAIAdapter
from app.collection.fetchers.plos_one import PLOSOneAdapter
from app.collection.fetchers.quantum_insider import QuantumInsiderAdapter
from app.collection.fetchers.spaceflight_now import SpaceflightNowAdapter
from app.collection.fetchers.spacenews import SpaceNewsAdapter
from app.collection.fetchers.techcrunch import TechCrunchAdapter
from app.collection.fetchers.the_register import TheRegisterAdapter
from app.collection.fetchers.tools.fetched_article import SourceAdapter
from app.collection.fetchers.tools.multi_feed_rss import MultiFeedRssAdapter
from app.collection.fetchers.tools.rss_parser import RssEntry, normalize_entry
from app.collection.fetchers.venturebeat import VentureBeatAdapter
from tests.collection.fetchers._invariant import (
    Passport,
    assert_at_least_one_passport,
    assert_passport_types_allowed,
    assert_passport_types_include,
    assert_passports_persistable,
)

_FIXTURES_DIR = Path(__file__).parent.parent.parent / "fixtures"


class _FixtureRssParser:
    """``RssParser`` の構造的 fake。fixture を feedparser で読み、
    ``normalize_entry`` を通して本番経路と同じ ``RssEntry`` を返す。

    ``parse_mode`` / ``endpoint_url`` / ``source_name`` は受け取って無視する
    (fixture は静的バイナリなので encoding 差異を再現する必要がない)。本物の
    ``RssParser.fetch`` と同じ kw シグネチャを満たすため ``**_`` で耐える。
    """

    def __init__(self, fixture_filename: str) -> None:
        self._fixture_filename = fixture_filename

    async def fetch(
        self,
        *,
        endpoint_url: str,
        source_name: str,
        parse_mode: str = "text",
        **_: object,
    ) -> list[RssEntry]:
        path = _FIXTURES_DIR / self._fixture_filename
        feed = feedparser.parse(path.read_bytes())
        return [normalize_entry(raw) for raw in feed.entries]


# AdapterBuilder = parser を受け取り machinery を返す callable。
AdapterBuilder = Callable[[_FixtureRssParser], SourceAdapter]

# 旧 invariant test と同じ Ready/Incomplete 集合表記。
_R_BODY_TRUSTED = {AnalyzableArticle, ObservedArticle}
_H_BODY_DISTRUSTED = {ObservedArticle}


def _standalone(cls: type, *, endpoint_url: str, source_name: str) -> AdapterBuilder:
    return lambda parser: cls(
        endpoint_url=endpoint_url, source_name=source_name, parser=parser
    )


# (label, builder, fixture_filename, allowed_types, must_include_types)
_CASES: list[tuple[str, AdapterBuilder, str, set[type], set[type]]] = [
    (
        "VentureBeat-full",
        _standalone(
            VentureBeatAdapter,
            endpoint_url="https://venturebeat.com/feed",
            source_name="VentureBeat",
        ),
        "venturebeat_rss.xml",
        _R_BODY_TRUSTED,
        {AnalyzableArticle},
    ),
    (
        "VentureBeat-teaser",
        _standalone(
            VentureBeatAdapter,
            endpoint_url="https://venturebeat.com/feed",
            source_name="VentureBeat",
        ),
        "venturebeat_teaser_rss.xml",
        {ObservedArticle},
        {ObservedArticle},
    ),
    (
        "TechCrunch",
        _standalone(
            TechCrunchAdapter,
            endpoint_url="https://techcrunch.com/feed/",
            source_name="TechCrunch",
        ),
        "techcrunch_rss.xml",
        _H_BODY_DISTRUSTED,
        {ObservedArticle},
    ),
    (
        "CleanTechnica",
        _standalone(
            CleanTechnicaAdapter,
            endpoint_url="https://cleantechnica.com/feed/",
            source_name="CleanTechnica",
        ),
        "cleantechnica_rss.xml",
        _H_BODY_DISTRUSTED,
        {ObservedArticle},
    ),
    (
        "DeepMind",
        _standalone(
            DeepMindAdapter,
            endpoint_url="https://deepmind.google/blog/rss.xml",
            source_name="Google DeepMind",
        ),
        "deepmind_rss.xml",
        _H_BODY_DISTRUSTED,
        {ObservedArticle},
    ),
    (
        "EETimesJapan",
        _standalone(
            EETimesJapanAdapter,
            endpoint_url="https://rss.itmedia.co.jp/rss/2.0/eetimes.xml",
            source_name="EE Times Japan",
        ),
        "eetimes_japan_rss.xml",
        _H_BODY_DISTRUSTED,
        {ObservedArticle},
    ),
    (
        "Electrek",
        _standalone(
            ElectrekAdapter,
            endpoint_url="https://electrek.co/feed/",
            source_name="Electrek",
        ),
        "electrek_rss.xml",
        _H_BODY_DISTRUSTED,
        {ObservedArticle},
    ),
    (
        "Engadget",
        _standalone(
            EngadgetAdapter,
            endpoint_url="https://www.engadget.com/rss.xml",
            source_name="Engadget",
        ),
        "engadget_rss.xml",
        _H_BODY_DISTRUSTED,
        {ObservedArticle},
    ),
    (
        "HuggingFace",
        _standalone(
            HuggingFaceBlogAdapter,
            endpoint_url="https://huggingface.co/blog/feed.xml",
            source_name="Hugging Face",
        ),
        "huggingface_blog_rss.xml",
        _H_BODY_DISTRUSTED,
        {ObservedArticle},
    ),
    (
        "ITmediaAI",
        _standalone(
            ITmediaAIAdapter,
            endpoint_url="https://rss.itmedia.co.jp/rss/2.0/aiplus.xml",
            source_name="ITmedia AI+",
        ),
        "itmedia_ai_rss.xml",
        _H_BODY_DISTRUSTED,
        {ObservedArticle},
    ),
    (
        "ITmediaNews",
        _standalone(
            ITmediaNewsAdapter,
            endpoint_url="https://rss.itmedia.co.jp/rss/2.0/news_bursts.xml",
            source_name="ITmedia NEWS",
        ),
        "itmedia_news_rss.xml",
        _H_BODY_DISTRUSTED,
        {ObservedArticle},
    ),
    (
        "JPCERT",
        _standalone(
            JPCERTAdapter,
            endpoint_url="https://www.jpcert.or.jp/rss/jpcert.rdf",
            source_name="JPCERT/CC",
        ),
        "jpcert_rss.xml",
        _H_BODY_DISTRUSTED,
        {ObservedArticle},
    ),
    (
        "METI",
        _standalone(
            METIAdapter,
            endpoint_url="https://www.meti.go.jp/ml_index_release_atom.xml",
            source_name="METI",
        ),
        "meti_atom.xml",
        _H_BODY_DISTRUSTED,
        {ObservedArticle},
    ),
    (
        "MEXT",
        _standalone(
            MEXTAdapter,
            endpoint_url="https://www.mext.go.jp/b_menu/news/index.rdf",
            source_name="MEXT",
        ),
        "mext_rdf.xml",
        _H_BODY_DISTRUSTED,
        {ObservedArticle},
    ),
    (
        "MIC",
        _standalone(
            MICAdapter,
            endpoint_url="https://www.soumu.go.jp/news.rdf",
            source_name="MIC",
        ),
        "mic_rdf.xml",
        _H_BODY_DISTRUSTED,
        {ObservedArticle},
    ),
    (
        "MONOist",
        _standalone(
            MONOistAdapter,
            endpoint_url="https://rss.itmedia.co.jp/rss/2.0/monoist.xml",
            source_name="MONOist",
        ),
        "monoist_rss.xml",
        _H_BODY_DISTRUSTED,
        {ObservedArticle},
    ),
    (
        "NIST",
        _standalone(
            NISTAdapter,
            endpoint_url="https://www.nist.gov/news-events/news/rss.xml",
            source_name="NIST",
        ),
        "nist_rss.xml",
        _H_BODY_DISTRUSTED,
        {ObservedArticle},
    ),
    (
        "NSF",
        _standalone(
            NSFAdapter,
            endpoint_url="https://www.nsf.gov/rss/rss_www_news.xml",
            source_name="NSF",
        ),
        "nsf_rss.xml",
        _H_BODY_DISTRUSTED,
        {ObservedArticle},
    ),
    (
        "OpenAI",
        _standalone(
            OpenAIAdapter,
            endpoint_url="https://openai.com/news/rss.xml",
            source_name="OpenAI",
        ),
        "openai_rss.xml",
        _H_BODY_DISTRUSTED,
        {ObservedArticle},
    ),
    (
        "SpaceNews",
        _standalone(
            SpaceNewsAdapter,
            endpoint_url="https://spacenews.com/feed/",
            source_name="SpaceNews",
        ),
        "spacenews_rss.xml",
        _H_BODY_DISTRUSTED,
        {ObservedArticle},
    ),
    (
        "CloudflareBlog",
        _standalone(
            CloudflareBlogAdapter,
            endpoint_url="https://blog.cloudflare.com/rss/",
            source_name="The Cloudflare Blog",
        ),
        "cloudflare_rss.xml",
        _R_BODY_TRUSTED,
        {AnalyzableArticle},
    ),
    (
        "ELife",
        _standalone(
            ELifeAdapter,
            endpoint_url="https://elifesciences.org/rss/recent.xml",
            source_name="eLife",
        ),
        "elife_rss.xml",
        _R_BODY_TRUSTED,
        {AnalyzableArticle},
    ),
    (
        "IEEESpectrum",
        _standalone(
            IEEESpectrumAdapter,
            endpoint_url="https://spectrum.ieee.org/feeds/feed.rss",
            source_name="IEEE Spectrum",
        ),
        "ieee_spectrum_rss.xml",
        _R_BODY_TRUSTED,
        {AnalyzableArticle},
    ),
    (
        "PLOSOne",
        _standalone(
            PLOSOneAdapter,
            endpoint_url="https://journals.plos.org/plosone/feed/atom",
            source_name="PLOS ONE",
        ),
        "plos_one_atom.xml",
        _R_BODY_TRUSTED,
        {AnalyzableArticle},
    ),
    (
        "QuantumInsider",
        _standalone(
            QuantumInsiderAdapter,
            endpoint_url="https://thequantuminsider.com/feed/",
            source_name="The Quantum Insider",
        ),
        "quantum_insider_rss.xml",
        _R_BODY_TRUSTED,
        {AnalyzableArticle},
    ),
    (
        "KrebsOnSecurity",
        _standalone(
            KrebsOnSecurityAdapter,
            endpoint_url="https://krebsonsecurity.com/feed/",
            source_name="Krebs on Security",
        ),
        "krebs_on_security_rss.xml",
        _R_BODY_TRUSTED,
        {AnalyzableArticle},
    ),
    (
        "SpaceflightNow",
        _standalone(
            SpaceflightNowAdapter,
            endpoint_url="https://spaceflightnow.com/feed/",
            source_name="Spaceflight Now",
        ),
        "spaceflight_now_rss.xml",
        _R_BODY_TRUSTED,
        {AnalyzableArticle},
    ),
    (
        "NASA",
        lambda parser: MultiFeedRssAdapter(
            source_name="NASA",
            feeds=NASA_FEEDS,
            parse_mode="text",
            body_builder=nasa_build_body,
            parser=parser,
        ),
        "nasa_rss.xml",
        _R_BODY_TRUSTED,
        {AnalyzableArticle},
    ),
    (
        "MetaAI",
        _standalone(
            MetaAIAdapter,
            endpoint_url="https://about.fb.com/news/feed/",
            source_name="Meta AI",
        ),
        "meta_ai_rss.xml",
        _R_BODY_TRUSTED,
        {AnalyzableArticle},
    ),
    (
        "MicrosoftResearch",
        _standalone(
            MicrosoftResearchAdapter,
            endpoint_url="https://www.microsoft.com/en-us/research/feed/",
            source_name="Microsoft Research",
        ),
        "microsoft_research_rss.xml",
        _R_BODY_TRUSTED,
        {AnalyzableArticle},
    ),
    (
        "FrontiersAI",
        lambda parser: FrontiersJournalAdapter(
            source_name="Frontiers in Artificial Intelligence",
            endpoint_url=(
                "https://www.frontiersin.org/journals/artificial-intelligence/rss"
            ),
            parser=parser,
        ),
        "frontiers_ai_rss.xml",
        _R_BODY_TRUSTED,
        {AnalyzableArticle},
    ),
    (
        "FrontiersRoboticsAI",
        lambda parser: FrontiersJournalAdapter(
            source_name="Frontiers in Robotics and AI",
            endpoint_url="https://www.frontiersin.org/journals/robotics-and-ai/rss",
            parser=parser,
        ),
        "frontiers_ai_rss.xml",
        _R_BODY_TRUSTED,
        {AnalyzableArticle},
    ),
    (
        "FrontiersEnergyResearch",
        lambda parser: FrontiersJournalAdapter(
            source_name="Frontiers in Energy Research",
            endpoint_url="https://www.frontiersin.org/journals/energy-research/rss",
            parser=parser,
        ),
        "frontiers_ai_rss.xml",
        _R_BODY_TRUSTED,
        {AnalyzableArticle},
    ),
    (
        "FrontiersMaterials",
        lambda parser: FrontiersJournalAdapter(
            source_name="Frontiers in Materials",
            endpoint_url="https://www.frontiersin.org/journals/materials/rss",
            parser=parser,
        ),
        "frontiers_ai_rss.xml",
        _R_BODY_TRUSTED,
        {AnalyzableArticle},
    ),
    (
        "CornellChronicle",
        lambda parser: MultiFeedRssAdapter(
            source_name="Cornell Chronicle",
            feeds=CORNELL_FEEDS,
            parse_mode="bytes",
            parser=parser,
        ),
        "cornell_rss.xml",
        _H_BODY_DISTRUSTED,
        {ObservedArticle},
    ),
    (
        "TheRegister",
        _standalone(
            TheRegisterAdapter,
            endpoint_url="https://www.theregister.com/headlines.atom",
            source_name="The Register",
        ),
        "the_register_atom.xml",
        _H_BODY_DISTRUSTED,
        {ObservedArticle},
    ),
    (
        "FierceBiotech",
        _standalone(
            FierceBiotechAdapter,
            endpoint_url="https://www.fiercebiotech.com/rss/xml",
            source_name="FierceBiotech",
        ),
        "fierce_biotech_rss.xml",
        _H_BODY_DISTRUSTED,
        {ObservedArticle},
    ),
    (
        "ESAHubble",
        lambda parser: DjangoplicityAdapter(
            source_name="ESA/Hubble",
            endpoint_url="https://esahubble.org/news/feed/",
            parser=parser,
        ),
        "esa_hubble_rss.xml",
        _H_BODY_DISTRUSTED,
        {ObservedArticle},
    ),
    (
        "ESAWebb",
        lambda parser: DjangoplicityAdapter(
            source_name="ESA/Webb",
            endpoint_url="https://esawebb.org/news/feed/",
            parser=parser,
        ),
        "esa_webb_rss.xml",
        _H_BODY_DISTRUSTED,
        {ObservedArticle},
    ),
]


async def _collect_passports(
    builder: AdapterBuilder, fixture_filename: str
) -> list[Passport]:
    """``ArticleFetcher`` 経由で fixture を流し passport を集める。

    machinery を fixture parser 注入で直構築し、薄い ``_SourceShim`` 経由で
    ``ArticleFetcher`` の本番経路 (passport_builder) に通す。
    """
    from app.collection.domain.observed_article import ObservedOrigin
    from app.collection.domain.source_completion_profile import DEFAULT_PROFILE
    from app.collection.sources.article_source import ArticleSource
    from app.shared.value_objects.source_name import SourceName

    adapter = builder(_FixtureRssParser(fixture_filename))
    # RSS 群は全て DEFAULT_PROFILE (feed origin)。仮タイトル source の
    # HTML_TITLE_PROFILE 経路は test_anthropic / test_ornl が担う。
    source = ArticleSource(
        name=SourceName("fixture"),
        endpoint_url="https://example.test/feed",
        observed_origin=ObservedOrigin.feed,
        completion_profile=DEFAULT_PROFILE,
        adapter_factory=lambda: adapter,
    )
    fetcher = ArticleFetcher(source)
    items: AsyncIterator[Passport] = fetcher.fetch(source_id=1)
    return [item async for item in items]


@pytest.fixture(params=_CASES, ids=lambda c: f"{c[0]}-{c[2]}")
async def case(
    request: pytest.FixtureRequest,
) -> tuple[list[Passport], set[type], set[type]]:
    _label, builder, fixture_name, allowed, must_include = request.param
    passports = await _collect_passports(builder, fixture_name)
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
