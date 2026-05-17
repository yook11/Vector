"""``ArticleSource`` 集約レジストリ (composition root, P2)。

collection-acquisition-redesign Phase 1 → fetcher big-bang → P2 Source 集約化
完結。全 45 ソースが「1 つの ``ArticleSource`` が (a) どう fetch するか=
``adapter_factory`` 経由の machinery、(b) どう完成させるか=
``completion_profile`` を所有する」形に収束し、本ファイルが唯一の dispatch
エントリポイントとなる (``ingest_source`` task で参照される)。

設計判断:

- env / Settings を読まず hardcode (Pure DI)
- 判定キーは ``news_sources.name`` (= ``ArticleSource.name``) — id は環境差で
  揺れ得るため
- per-source 知識 (補完方針 / 取得出自 / identity) は ``ArticleSource``
  インスタンスが所有する。``SOURCES`` は ``SourceName → ArticleSource`` の
  レジストリで、Stage 2 の ``CompletionProfileResolver`` が **無 instantiation**
  で ``.completion_profile`` を引くために参照する (``adapter_factory`` は
  遅延 callable のため、レジストリ構築 = module import 時に ``RssParser()`` /
  ``CrossrefApiClient()`` 等の machinery は構築されない。profile 解決は
  ``completion_profile`` フィールド直読みで ``make_adapter()`` を呼ばない =
  無 instantiation 契約を **設計** で担保。spec §4.6 ガードレール)
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

from app.collection.domain.observed_article import ObservedOrigin
from app.collection.domain.source_completion_profile import (
    DEFAULT_PROFILE,
    HTML_TITLE_PROFILE,
)
from app.collection.fetchers.anthropic import AnthropicAdapter
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
from app.collection.fetchers.hacker_news import HackerNewsAdapter
from app.collection.fetchers.huggingface import HuggingFaceBlogAdapter
from app.collection.fetchers.ieee_spectrum import IEEESpectrumAdapter
from app.collection.fetchers.itmedia_ai import ITmediaAIAdapter
from app.collection.fetchers.itmedia_news import ITmediaNewsAdapter
from app.collection.fetchers.jpcert import JPCERTAdapter
from app.collection.fetchers.krebs_on_security import KrebsOnSecurityAdapter
from app.collection.fetchers.mdpi._common import MDPICrossrefAdapter
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
from app.collection.fetchers.ornl import ORNLAdapter
from app.collection.fetchers.plos_one import PLOSOneAdapter
from app.collection.fetchers.protocol import Fetcher
from app.collection.fetchers.quantum_insider import QuantumInsiderAdapter
from app.collection.fetchers.spaceflight_now import SpaceflightNowAdapter
from app.collection.fetchers.spacenews import SpaceNewsAdapter
from app.collection.fetchers.techcrunch import TechCrunchAdapter
from app.collection.fetchers.the_register import TheRegisterAdapter
from app.collection.fetchers.tools.multi_feed_rss import MultiFeedRssAdapter
from app.collection.fetchers.venturebeat import VentureBeatAdapter
from app.collection.sources.article_source import ArticleSource
from app.shared.value_objects.source_name import SourceName

# 1 ニュースソース = 1 ``ArticleSource``。順序は P1 時点の登録順を踏襲
# (``FETCHERS`` の iteration order を byte 不変に保つ)。``adapter_factory`` は
# 遅延 callable: module import 時に machinery (RssParser / CrossrefApiClient)
# は構築されない (無 instantiation 契約)。
_SOURCES_LIST: Final[tuple[ArticleSource, ...]] = (
    ArticleSource(
        name=SourceName("VentureBeat"),
        endpoint_url="https://venturebeat.com/feed",
        observed_origin=ObservedOrigin.feed,
        completion_profile=DEFAULT_PROFILE,
        adapter_factory=lambda: VentureBeatAdapter(
            endpoint_url="https://venturebeat.com/feed", source_name="VentureBeat"
        ),
    ),
    ArticleSource(
        name=SourceName("TechCrunch"),
        endpoint_url="https://techcrunch.com/feed/",
        observed_origin=ObservedOrigin.feed,
        completion_profile=DEFAULT_PROFILE,
        adapter_factory=lambda: TechCrunchAdapter(
            endpoint_url="https://techcrunch.com/feed/", source_name="TechCrunch"
        ),
    ),
    ArticleSource(
        name=SourceName("The Quantum Insider"),
        endpoint_url="https://thequantuminsider.com/feed/",
        observed_origin=ObservedOrigin.feed,
        completion_profile=DEFAULT_PROFILE,
        adapter_factory=lambda: QuantumInsiderAdapter(
            endpoint_url="https://thequantuminsider.com/feed/",
            source_name="The Quantum Insider",
        ),
    ),
    ArticleSource(
        name=SourceName("Krebs on Security"),
        endpoint_url="https://krebsonsecurity.com/feed/",
        observed_origin=ObservedOrigin.feed,
        completion_profile=DEFAULT_PROFILE,
        adapter_factory=lambda: KrebsOnSecurityAdapter(
            endpoint_url="https://krebsonsecurity.com/feed/",
            source_name="Krebs on Security",
        ),
    ),
    ArticleSource(
        name=SourceName("Spaceflight Now"),
        endpoint_url="https://spaceflightnow.com/feed/",
        observed_origin=ObservedOrigin.feed,
        completion_profile=DEFAULT_PROFILE,
        adapter_factory=lambda: SpaceflightNowAdapter(
            endpoint_url="https://spaceflightnow.com/feed/",
            source_name="Spaceflight Now",
        ),
    ),
    ArticleSource(
        name=SourceName("NASA"),
        endpoint_url="https://www.nasa.gov/feed/",
        observed_origin=ObservedOrigin.feed,
        completion_profile=DEFAULT_PROFILE,
        adapter_factory=lambda: MultiFeedRssAdapter(
            source_name="NASA",
            feeds=NASA_FEEDS,
            parse_mode="text",
            body_builder=nasa_build_body,
        ),
    ),
    ArticleSource(
        name=SourceName("IEEE Spectrum"),
        endpoint_url="https://spectrum.ieee.org/feeds/feed.rss",
        observed_origin=ObservedOrigin.feed,
        completion_profile=DEFAULT_PROFILE,
        adapter_factory=lambda: IEEESpectrumAdapter(
            endpoint_url="https://spectrum.ieee.org/feeds/feed.rss",
            source_name="IEEE Spectrum",
        ),
    ),
    ArticleSource(
        name=SourceName("Microsoft Research"),
        endpoint_url="https://www.microsoft.com/en-us/research/feed/",
        observed_origin=ObservedOrigin.feed,
        completion_profile=DEFAULT_PROFILE,
        adapter_factory=lambda: MicrosoftResearchAdapter(
            endpoint_url="https://www.microsoft.com/en-us/research/feed/",
            source_name="Microsoft Research",
        ),
    ),
    ArticleSource(
        name=SourceName("ITmedia AI+"),
        endpoint_url="https://rss.itmedia.co.jp/rss/2.0/aiplus.xml",
        observed_origin=ObservedOrigin.feed,
        completion_profile=DEFAULT_PROFILE,
        adapter_factory=lambda: ITmediaAIAdapter(
            endpoint_url="https://rss.itmedia.co.jp/rss/2.0/aiplus.xml",
            source_name="ITmedia AI+",
        ),
    ),
    ArticleSource(
        name=SourceName("ITmedia NEWS"),
        endpoint_url="https://rss.itmedia.co.jp/rss/2.0/news_bursts.xml",
        observed_origin=ObservedOrigin.feed,
        completion_profile=DEFAULT_PROFILE,
        adapter_factory=lambda: ITmediaNewsAdapter(
            endpoint_url="https://rss.itmedia.co.jp/rss/2.0/news_bursts.xml",
            source_name="ITmedia NEWS",
        ),
    ),
    ArticleSource(
        name=SourceName("MONOist"),
        endpoint_url="https://rss.itmedia.co.jp/rss/2.0/monoist.xml",
        observed_origin=ObservedOrigin.feed,
        completion_profile=DEFAULT_PROFILE,
        adapter_factory=lambda: MONOistAdapter(
            endpoint_url="https://rss.itmedia.co.jp/rss/2.0/monoist.xml",
            source_name="MONOist",
        ),
    ),
    ArticleSource(
        name=SourceName("EE Times Japan"),
        endpoint_url="https://rss.itmedia.co.jp/rss/2.0/eetimes.xml",
        observed_origin=ObservedOrigin.feed,
        completion_profile=DEFAULT_PROFILE,
        adapter_factory=lambda: EETimesJapanAdapter(
            endpoint_url="https://rss.itmedia.co.jp/rss/2.0/eetimes.xml",
            source_name="EE Times Japan",
        ),
    ),
    ArticleSource(
        name=SourceName("Engadget"),
        endpoint_url="https://www.engadget.com/rss.xml",
        observed_origin=ObservedOrigin.feed,
        completion_profile=DEFAULT_PROFILE,
        adapter_factory=lambda: EngadgetAdapter(
            endpoint_url="https://www.engadget.com/rss.xml", source_name="Engadget"
        ),
    ),
    ArticleSource(
        name=SourceName("FierceBiotech"),
        endpoint_url="https://www.fiercebiotech.com/rss/xml",
        observed_origin=ObservedOrigin.feed,
        completion_profile=DEFAULT_PROFILE,
        adapter_factory=lambda: FierceBiotechAdapter(
            endpoint_url="https://www.fiercebiotech.com/rss/xml",
            source_name="FierceBiotech",
        ),
    ),
    ArticleSource(
        name=SourceName("JPCERT/CC"),
        endpoint_url="https://www.jpcert.or.jp/rss/jpcert.rdf",
        observed_origin=ObservedOrigin.feed,
        completion_profile=DEFAULT_PROFILE,
        adapter_factory=lambda: JPCERTAdapter(
            endpoint_url="https://www.jpcert.or.jp/rss/jpcert.rdf",
            source_name="JPCERT/CC",
        ),
    ),
    ArticleSource(
        name=SourceName("CleanTechnica"),
        endpoint_url="https://cleantechnica.com/feed/",
        observed_origin=ObservedOrigin.feed,
        completion_profile=DEFAULT_PROFILE,
        adapter_factory=lambda: CleanTechnicaAdapter(
            endpoint_url="https://cleantechnica.com/feed/",
            source_name="CleanTechnica",
        ),
    ),
    ArticleSource(
        name=SourceName("Electrek"),
        endpoint_url="https://electrek.co/feed/",
        observed_origin=ObservedOrigin.feed,
        completion_profile=DEFAULT_PROFILE,
        adapter_factory=lambda: ElectrekAdapter(
            endpoint_url="https://electrek.co/feed/", source_name="Electrek"
        ),
    ),
    ArticleSource(
        name=SourceName("SpaceNews"),
        endpoint_url="https://spacenews.com/feed/",
        observed_origin=ObservedOrigin.feed,
        completion_profile=DEFAULT_PROFILE,
        adapter_factory=lambda: SpaceNewsAdapter(
            endpoint_url="https://spacenews.com/feed/", source_name="SpaceNews"
        ),
    ),
    ArticleSource(
        name=SourceName("The Register"),
        endpoint_url="https://www.theregister.com/headlines.atom",
        observed_origin=ObservedOrigin.feed,
        completion_profile=DEFAULT_PROFILE,
        adapter_factory=lambda: TheRegisterAdapter(
            endpoint_url="https://www.theregister.com/headlines.atom",
            source_name="The Register",
        ),
    ),
    ArticleSource(
        name=SourceName("Hacker News"),
        endpoint_url="https://hn.algolia.com/api/v1/search_by_date",
        observed_origin=ObservedOrigin.api,
        completion_profile=DEFAULT_PROFILE,
        adapter_factory=lambda: HackerNewsAdapter(source_name="Hacker News"),
    ),
    ArticleSource(
        name=SourceName("MEXT"),
        endpoint_url="https://www.mext.go.jp/b_menu/news/index.rdf",
        observed_origin=ObservedOrigin.feed,
        completion_profile=DEFAULT_PROFILE,
        adapter_factory=lambda: MEXTAdapter(
            endpoint_url="https://www.mext.go.jp/b_menu/news/index.rdf",
            source_name="MEXT",
        ),
    ),
    ArticleSource(
        name=SourceName("MIC"),
        endpoint_url="https://www.soumu.go.jp/news.rdf",
        observed_origin=ObservedOrigin.feed,
        completion_profile=DEFAULT_PROFILE,
        adapter_factory=lambda: MICAdapter(
            endpoint_url="https://www.soumu.go.jp/news.rdf", source_name="MIC"
        ),
    ),
    ArticleSource(
        name=SourceName("METI"),
        endpoint_url="https://www.meti.go.jp/ml_index_release_atom.xml",
        observed_origin=ObservedOrigin.feed,
        completion_profile=DEFAULT_PROFILE,
        adapter_factory=lambda: METIAdapter(
            endpoint_url="https://www.meti.go.jp/ml_index_release_atom.xml",
            source_name="METI",
        ),
    ),
    ArticleSource(
        name=SourceName("Anthropic"),
        endpoint_url="https://www.anthropic.com/sitemap.xml",
        observed_origin=ObservedOrigin.sitemap,
        completion_profile=HTML_TITLE_PROFILE,
        adapter_factory=lambda: AnthropicAdapter(
            endpoint_url="https://www.anthropic.com/sitemap.xml",
            source_name="Anthropic",
        ),
    ),
    ArticleSource(
        name=SourceName("NIST"),
        endpoint_url="https://www.nist.gov/news-events/news/rss.xml",
        observed_origin=ObservedOrigin.feed,
        completion_profile=DEFAULT_PROFILE,
        adapter_factory=lambda: NISTAdapter(
            endpoint_url="https://www.nist.gov/news-events/news/rss.xml",
            source_name="NIST",
        ),
    ),
    ArticleSource(
        name=SourceName("NSF"),
        endpoint_url="https://www.nsf.gov/rss/rss_www_news.xml",
        observed_origin=ObservedOrigin.feed,
        completion_profile=DEFAULT_PROFILE,
        adapter_factory=lambda: NSFAdapter(
            endpoint_url="https://www.nsf.gov/rss/rss_www_news.xml",
            source_name="NSF",
        ),
    ),
    ArticleSource(
        name=SourceName("The Cloudflare Blog"),
        endpoint_url="https://blog.cloudflare.com/rss/",
        observed_origin=ObservedOrigin.feed,
        completion_profile=DEFAULT_PROFILE,
        adapter_factory=lambda: CloudflareBlogAdapter(
            endpoint_url="https://blog.cloudflare.com/rss/",
            source_name="The Cloudflare Blog",
        ),
    ),
    ArticleSource(
        name=SourceName("Google DeepMind"),
        endpoint_url="https://deepmind.google/blog/rss.xml",
        observed_origin=ObservedOrigin.feed,
        completion_profile=DEFAULT_PROFILE,
        adapter_factory=lambda: DeepMindAdapter(
            endpoint_url="https://deepmind.google/blog/rss.xml",
            source_name="Google DeepMind",
        ),
    ),
    ArticleSource(
        name=SourceName("ESA/Hubble"),
        endpoint_url="https://esahubble.org/news/feed/",
        observed_origin=ObservedOrigin.feed,
        completion_profile=DEFAULT_PROFILE,
        adapter_factory=lambda: DjangoplicityAdapter(
            source_name="ESA/Hubble",
            endpoint_url="https://esahubble.org/news/feed/",
        ),
    ),
    ArticleSource(
        name=SourceName("ESA/Webb"),
        endpoint_url="https://esawebb.org/news/feed/",
        observed_origin=ObservedOrigin.feed,
        completion_profile=DEFAULT_PROFILE,
        adapter_factory=lambda: DjangoplicityAdapter(
            source_name="ESA/Webb",
            endpoint_url="https://esawebb.org/news/feed/",
        ),
    ),
    ArticleSource(
        name=SourceName("OpenAI"),
        endpoint_url="https://openai.com/news/rss.xml",
        observed_origin=ObservedOrigin.feed,
        completion_profile=DEFAULT_PROFILE,
        adapter_factory=lambda: OpenAIAdapter(
            endpoint_url="https://openai.com/news/rss.xml", source_name="OpenAI"
        ),
    ),
    ArticleSource(
        name=SourceName("Hugging Face"),
        endpoint_url="https://huggingface.co/blog/feed.xml",
        observed_origin=ObservedOrigin.feed,
        completion_profile=DEFAULT_PROFILE,
        adapter_factory=lambda: HuggingFaceBlogAdapter(
            endpoint_url="https://huggingface.co/blog/feed.xml",
            source_name="Hugging Face",
        ),
    ),
    ArticleSource(
        name=SourceName("eLife"),
        endpoint_url="https://elifesciences.org/rss/recent.xml",
        observed_origin=ObservedOrigin.feed,
        completion_profile=DEFAULT_PROFILE,
        adapter_factory=lambda: ELifeAdapter(
            endpoint_url="https://elifesciences.org/rss/recent.xml",
            source_name="eLife",
        ),
    ),
    ArticleSource(
        name=SourceName("PLOS ONE"),
        endpoint_url="https://journals.plos.org/plosone/feed/atom",
        observed_origin=ObservedOrigin.feed,
        completion_profile=DEFAULT_PROFILE,
        adapter_factory=lambda: PLOSOneAdapter(
            endpoint_url="https://journals.plos.org/plosone/feed/atom",
            source_name="PLOS ONE",
        ),
    ),
    ArticleSource(
        name=SourceName("Meta AI"),
        endpoint_url="https://about.fb.com/news/feed/",
        observed_origin=ObservedOrigin.feed,
        completion_profile=DEFAULT_PROFILE,
        adapter_factory=lambda: MetaAIAdapter(
            endpoint_url="https://about.fb.com/news/feed/", source_name="Meta AI"
        ),
    ),
    ArticleSource(
        name=SourceName("Cornell Chronicle"),
        endpoint_url="https://news.cornell.edu/taxonomy/term/24043/feed",
        observed_origin=ObservedOrigin.feed,
        completion_profile=DEFAULT_PROFILE,
        adapter_factory=lambda: MultiFeedRssAdapter(
            source_name="Cornell Chronicle",
            feeds=CORNELL_FEEDS,
            parse_mode="bytes",
        ),
    ),
    ArticleSource(
        name=SourceName("Frontiers in Artificial Intelligence"),
        endpoint_url="https://www.frontiersin.org/journals/artificial-intelligence/rss",
        observed_origin=ObservedOrigin.feed,
        completion_profile=DEFAULT_PROFILE,
        adapter_factory=lambda: FrontiersJournalAdapter(
            source_name="Frontiers in Artificial Intelligence",
            endpoint_url=(
                "https://www.frontiersin.org/journals/artificial-intelligence/rss"
            ),
        ),
    ),
    ArticleSource(
        name=SourceName("Frontiers in Robotics and AI"),
        endpoint_url="https://www.frontiersin.org/journals/robotics-and-ai/rss",
        observed_origin=ObservedOrigin.feed,
        completion_profile=DEFAULT_PROFILE,
        adapter_factory=lambda: FrontiersJournalAdapter(
            source_name="Frontiers in Robotics and AI",
            endpoint_url="https://www.frontiersin.org/journals/robotics-and-ai/rss",
        ),
    ),
    ArticleSource(
        name=SourceName("Frontiers in Energy Research"),
        endpoint_url="https://www.frontiersin.org/journals/energy-research/rss",
        observed_origin=ObservedOrigin.feed,
        completion_profile=DEFAULT_PROFILE,
        adapter_factory=lambda: FrontiersJournalAdapter(
            source_name="Frontiers in Energy Research",
            endpoint_url="https://www.frontiersin.org/journals/energy-research/rss",
        ),
    ),
    ArticleSource(
        name=SourceName("Frontiers in Materials"),
        endpoint_url="https://www.frontiersin.org/journals/materials/rss",
        observed_origin=ObservedOrigin.feed,
        completion_profile=DEFAULT_PROFILE,
        adapter_factory=lambda: FrontiersJournalAdapter(
            source_name="Frontiers in Materials",
            endpoint_url="https://www.frontiersin.org/journals/materials/rss",
        ),
    ),
    ArticleSource(
        name=SourceName("ORNL"),
        endpoint_url="https://www.ornl.gov/news",
        observed_origin=ObservedOrigin.listing,
        completion_profile=HTML_TITLE_PROFILE,
        adapter_factory=lambda: ORNLAdapter(
            endpoint_url="https://www.ornl.gov/news", source_name="ORNL"
        ),
    ),
    ArticleSource(
        name=SourceName("MDPI Materials"),
        endpoint_url="https://api.crossref.org/works",
        observed_origin=ObservedOrigin.feed,
        completion_profile=DEFAULT_PROFILE,
        adapter_factory=lambda: MDPICrossrefAdapter(
            source_name="MDPI Materials", issn="1996-1944"
        ),
    ),
    ArticleSource(
        name=SourceName("MDPI Energies"),
        endpoint_url="https://api.crossref.org/works",
        observed_origin=ObservedOrigin.feed,
        completion_profile=DEFAULT_PROFILE,
        adapter_factory=lambda: MDPICrossrefAdapter(
            source_name="MDPI Energies", issn="1996-1073"
        ),
    ),
    ArticleSource(
        name=SourceName("MDPI Sensors"),
        endpoint_url="https://api.crossref.org/works",
        observed_origin=ObservedOrigin.feed,
        completion_profile=DEFAULT_PROFILE,
        adapter_factory=lambda: MDPICrossrefAdapter(
            source_name="MDPI Sensors", issn="1424-8220"
        ),
    ),
    ArticleSource(
        name=SourceName("MDPI Nanomaterials"),
        endpoint_url="https://api.crossref.org/works",
        observed_origin=ObservedOrigin.feed,
        completion_profile=DEFAULT_PROFILE,
        adapter_factory=lambda: MDPICrossrefAdapter(
            source_name="MDPI Nanomaterials", issn="2079-4991"
        ),
    ),
)

# ``SourceName → ArticleSource`` レジストリ。Stage 2 resolver はここから
# 無 instantiation で ``.completion_profile`` / ``.observed_origin`` を引く。
SOURCES: Final[dict[SourceName, ArticleSource]] = {
    source.name: source for source in _SOURCES_LIST
}

# ``ingest_source`` task の ``FETCHERS[arg.name]`` 消費 (str キー) は無改修。
# ``SOURCES`` から導出することで「name→source」と「name→fetcher」の desync を
# 構造排除。``lambda s=s:`` の default 引数束縛で per-source 値を固定する。
FETCHERS: Final[dict[str, Callable[[], Fetcher]]] = {
    str(source.name): (lambda s=source: ArticleFetcher(s)) for source in _SOURCES_LIST
}
