"""Source 写像 totality 発見オラクル (R7/R8) — Step 0。

Reader Step 0 (``test_reader_role_contract.py``) と並ぶ Source 層の役割契約
発見オラクル。検証は普遍 / 実装は機構別 strangler。

## 軸

- **R7 (mapping totality)**: ``collect`` の yield 件数 == named scope を通った
  entry 件数 (scope predicate を持たない source は entry 件数そのまま)。
- **R8 (scope predicate naming)**: per-item の意図的除外があるなら public
  named callable ``is_collectable_<...>`` として export されている。implicit
  drop (``continue`` / private filter) は R8 違反。

R7 が直接観測軸。R8 は R7 で count mismatch が出たときの triage の物差し
(named なら R8 は緑、なければ implicit drop = R8 違反として赤化)。

## fixture invariants

オラクルが純粋に R7 を観測するため fixture は以下を満たす:

1. **cap 未発火**: 各 source の ``MAX_ENTRIES`` より entry 数が少ない
2. **dedup 未発火**: entries 内に dedup key (URL 等) の重複なし。ORNL は
   ``ornl_listing_for_oracle.html`` (dup-free variant) を使う。
3. **fan-out 配線済**: multi-feed source (NASA / Cornell) は per-feed fixture を
   ``endpoint_url`` で route する ``_FakeMultiFeedRssReader`` で配線済み、
   disjoint URL 前提で cross-feed dedup を未発火にし R7 を集約後 count parity で
   clean に観測する。同一 fixture を 6 feed replay すると cross-feed dedup が
   5 回発火し dedup invariant を破るため、合成された
   ``nasa_for_oracle_feed_a/b.xml`` / ``cornell_for_oracle_feed_a/b.xml`` で
   disjoint URL を担保する。per-feed 失敗隔離 (spec L82-83) は本オラクル外で
   層 2 責務。

これらが発火する観測は本オラクル外:

- cap / dedup の挙動境界 → per-source collect 正本テスト
  (``test_anthropic_adapter.py:122-143`` / ``test__common_adapter.py``)
- fan-out の per-feed 失敗隔離 (spec L82-83) → 層 2 の per-source 契約テスト

## 赤の triage は本オラクル外

赤 = 「(b1) 値欠落 implicit drop」または「(b2) 未 named な意図的 scope filter」
のどちらか。Step N の機構別 strangler で per-source に triage し処置する。
オラクルは count mismatch を SSoT 化するのみ。
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import feedparser
import pytest

from app.collection.article_collection.reader.algolia_hn_reader import (
    HackerNewsEntry,
    normalize_hit,
)
from app.collection.article_collection.reader.crossref_reader import (
    CrossrefEntry,
    normalize_item,
)
from app.collection.article_collection.reader.html_listing_reader import (
    HtmlListingEntry,
    HtmlListingReader,
)
from app.collection.article_collection.reader.rss_reader import (
    RssEntry,
    normalize_entry,
)
from app.collection.article_collection.reader.sitemap_reader import (
    SitemapEntry,
    SitemapReader,
)
from app.collection.article_collection.strategy import SOURCES
from app.collection.article_collection.tools.fetch_tools import FetchTools
from app.collection.article_collection.tools.raw_http_client import RawHttpClient
from app.collection.external_fetch_errors import ExternalFetchError
from app.collection.sources.article_source import ArticleSource
from app.collection.sources.definitions.anthropic import (
    AnthropicSource,
    is_collectable_anthropic_url,
)
from app.collection.sources.definitions.cleantechnica import CleanTechnicaSource
from app.collection.sources.definitions.cloudflare import CloudflareBlogSource
from app.collection.sources.definitions.cornell import (
    CORNELL_FEEDS,
    CornellChronicleSource,
)
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
    is_collectable_mdpi_work,
)
from app.collection.sources.definitions.meta_ai import (
    MetaAISource,
    is_collectable_meta_ai_entry,
)
from app.collection.sources.definitions.meti import METISource
from app.collection.sources.definitions.mext import MEXTSource
from app.collection.sources.definitions.mic import MICSource
from app.collection.sources.definitions.microsoft_research import (
    MicrosoftResearchSource,
)
from app.collection.sources.definitions.monoist import MONOistSource
from app.collection.sources.definitions.nasa import NASA_FEEDS, NASASource
from app.collection.sources.definitions.nist import NISTSource
from app.collection.sources.definitions.nsf import NSFSource
from app.collection.sources.definitions.openai import OpenAISource
from app.collection.sources.definitions.ornl import ORNLSource, is_collectable_ornl_url
from app.collection.sources.definitions.plos_one import PLOSOneSource
from app.collection.sources.definitions.quantum_insider import QuantumInsiderSource
from app.collection.sources.definitions.spaceflight_now import SpaceflightNowSource
from app.collection.sources.definitions.spacenews import SpaceNewsSource
from app.collection.sources.definitions.techcrunch import TechCrunchSource
from app.collection.sources.definitions.the_register import TheRegisterSource
from app.collection.sources.definitions.venturebeat import VentureBeatSource

_FIXTURES_DIR = Path(__file__).parent.parent.parent / "fixtures"


# ─── 機構別 fake transport ────────────────────────────────────────────────


class _FakeRawHttp(RawHttpClient):
    """``RawHttpClient`` の構造的 fake。同じ payload を返す (accept 無視)。"""

    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    async def fetch(self, *, url: str, source_name: str) -> bytes:  # noqa: ARG002
        return self._payload


class _FakeRssReader:
    """``RssReader`` の構造的 fake。fixture bytes を本物の ``normalize_entry``
    経路で ``RssEntry`` 列にする (``_FixtureRssReader`` と同思想だが parse mode
    を無視して 1 fixture 専用)。"""

    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    async def fetch(
        self,
        *,
        endpoint_url: str,  # noqa: ARG002
        source_name: str,  # noqa: ARG002
        parse_mode: str = "text",  # noqa: ARG002
        **_: object,
    ) -> list[RssEntry]:
        feed = feedparser.parse(self._payload)
        return [normalize_entry(raw) for raw in feed.entries]


class _FakeMultiFeedRssReader:
    """fan-out source 用 ``RssReader`` の構造的 fake。``endpoint_url`` で
    per-feed fixture を routing する。本オラクルでは fixture 全体で disjoint な
    URL を入れ cross-feed dedup を未発火にして R7 を純粋に観測する。map に無い
    endpoint は空 entries (``multi_feed_rss`` の per-feed 失敗隔離経路を踏まず
    「その feed には記事が無い」状態を再現)。"""

    def __init__(self, payloads_by_endpoint: dict[str, bytes]) -> None:
        self._payloads = payloads_by_endpoint

    async def fetch(
        self,
        *,
        endpoint_url: str,
        source_name: str,  # noqa: ARG002
        parse_mode: str = "text",  # noqa: ARG002
        **_: object,
    ) -> list[RssEntry]:
        payload = self._payloads.get(endpoint_url)
        if payload is None:
            return []
        feed = feedparser.parse(payload)
        return [normalize_entry(raw) for raw in feed.entries]


class _FakeCrossrefReader:
    """``CrossrefReader`` の構造的 fake。fixture JSON を本物の ``normalize_item``
    経路で ``CrossrefEntry`` 列にする。"""

    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    async def fetch_works(
        self,
        *,
        source_name: str,  # noqa: ARG002
        issn: str,  # noqa: ARG002
        from_pub_date: str,  # noqa: ARG002
        rows: int,  # noqa: ARG002
    ) -> list[CrossrefEntry]:
        data = json.loads(self._payload)
        items: list[dict[str, Any]] = list(data.get("message", {}).get("items", []))
        return [normalize_item(item) for item in items]


class _FakeHackerNewsReader:
    """``HackerNewsReader`` の構造的 fake。fixture JSON を本物の ``normalize_hit``
    経路で ``HackerNewsEntry`` 列にする。"""

    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    async def search_recent_stories(
        self,
        *,
        source_name: str,  # noqa: ARG002
        min_points: int,  # noqa: ARG002
        window_seconds: int,  # noqa: ARG002
        hits_per_page: int,  # noqa: ARG002
    ) -> list[HackerNewsEntry]:
        data = json.loads(self._payload)
        hits: list[dict[str, Any]] = list(data.get("hits", []))
        return [normalize_hit(hit) for hit in hits]


# ─── 機構別 case factory ──────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class _OracleCase:
    """1 source 用のオラクル case (entry 列 + 同 fixture を載せた tools)。"""

    tools: FetchTools
    entries: list[Any]


async def _rss_case(fixture_filename: str) -> _OracleCase:
    """RSS 機構: 同じ fixture bytes を Reader fake と entry 算出に両用。"""
    payload = (_FIXTURES_DIR / fixture_filename).read_bytes()
    fake = _FakeRssReader(payload)
    entries = await fake.fetch(endpoint_url="x", source_name="oracle")
    return _OracleCase(tools=FetchTools(rss=fake), entries=entries)  # type: ignore[arg-type]


async def _sitemap_case(fixture_filename: str) -> _OracleCase:
    """sitemap 機構: ``_FakeRawHttp`` を ``SitemapReader`` に wrap させて
    本物の defensive parse を通し ``SitemapEntry`` を得る。"""
    payload = (_FIXTURES_DIR / fixture_filename).read_bytes()
    http = _FakeRawHttp(payload)
    entries: list[SitemapEntry] = await SitemapReader(http=http).fetch(
        url="x", source_name="oracle"
    )
    tools = FetchTools(raw_http_factory=lambda _accept: http)
    return _OracleCase(tools=tools, entries=entries)


async def _html_listing_case(
    fixture_filename: str, *, detail_link_xpath: str
) -> _OracleCase:
    """HTML listing 機構: ``_FakeRawHttp`` を ``HtmlListingReader`` に wrap し
    本物の defensive parse + xpath を通して ``HtmlListingEntry`` を得る。"""
    payload = (_FIXTURES_DIR / fixture_filename).read_bytes()
    http = _FakeRawHttp(payload)
    entries: list[HtmlListingEntry] = await HtmlListingReader(http=http).fetch(
        url="x", source_name="oracle", detail_link_xpath=detail_link_xpath
    )
    tools = FetchTools(raw_http_factory=lambda _accept: http)
    return _OracleCase(tools=tools, entries=entries)


async def _crossref_case(fixture_filename: str) -> _OracleCase:
    payload = (_FIXTURES_DIR / fixture_filename).read_bytes()
    fake = _FakeCrossrefReader(payload)
    entries = await fake.fetch_works(
        source_name="oracle", issn="x", from_pub_date="2000-01-01", rows=20
    )
    return _OracleCase(tools=FetchTools(crossref=fake), entries=entries)  # type: ignore[arg-type]


async def _hn_case(fixture_filename: str) -> _OracleCase:
    payload = (_FIXTURES_DIR / fixture_filename).read_bytes()
    fake = _FakeHackerNewsReader(payload)
    entries = await fake.search_recent_stories(
        source_name="oracle", min_points=0, window_seconds=86400, hits_per_page=100
    )
    return _OracleCase(tools=FetchTools(hacker_news=fake), entries=entries)  # type: ignore[arg-type]


async def _multi_feed_rss_case(
    payloads_by_endpoint: dict[str, str],
) -> _OracleCase:
    """fan-out 機構: per-feed fixture を endpoint_url で route する fake を渡し、
    fixture 横断の全 entry を entries として集約する (disjoint URL 前提のため
    cross-feed dedup は no-op で count parity が clean に観測される)。"""
    payloads: dict[str, bytes] = {
        endpoint: (_FIXTURES_DIR / filename).read_bytes()
        for endpoint, filename in payloads_by_endpoint.items()
    }
    fake = _FakeMultiFeedRssReader(payloads)
    all_entries: list[RssEntry] = []
    for payload in payloads.values():
        feed = feedparser.parse(payload)
        all_entries.extend(normalize_entry(raw) for raw in feed.entries)
    return _OracleCase(tools=FetchTools(rss=fake), entries=all_entries)  # type: ignore[arg-type]


# ─── manifest: (source, scope_predicate, case_factory) ───────────────────


CaseFactory = Callable[[], Awaitable[_OracleCase]]


def _rss(filename: str) -> CaseFactory:
    return lambda: _rss_case(filename)


def _sitemap(filename: str) -> CaseFactory:
    return lambda: _sitemap_case(filename)


def _html_listing(filename: str, *, detail_link_xpath: str) -> CaseFactory:
    return lambda: _html_listing_case(filename, detail_link_xpath=detail_link_xpath)


def _crossref(filename: str) -> CaseFactory:
    return lambda: _crossref_case(filename)


def _hn(filename: str) -> CaseFactory:
    return lambda: _hn_case(filename)


def _multi_feed_rss(payloads_by_endpoint: dict[str, str]) -> CaseFactory:
    return lambda: _multi_feed_rss_case(payloads_by_endpoint)


# Source → scope predicate (named-public-contract 一覧の SSoT)。
# Step N で新 scope predicate を named 化したらここに 1 行追加する。
_SCOPE_PREDICATES: dict[ArticleSource, Callable[[Any], bool]] = {
    AnthropicSource: is_collectable_anthropic_url,
    ORNLSource: is_collectable_ornl_url,
    MDPIMaterialsSource: is_collectable_mdpi_work,
    MDPIEnergiesSource: is_collectable_mdpi_work,
    MDPISensorsSource: is_collectable_mdpi_work,
    MDPINanomaterialsSource: is_collectable_mdpi_work,
    MetaAISource: is_collectable_meta_ai_entry,
}


# 発見オラクルの 2 相運用: Phase 1 (新規発見) は xfail 無しの bare 状態で走らせ
# 赤を discovery、Phase 2 で発見済赤を xfail-strict + reason string で codify。
# Step N の機構別 strangler で drop 除去 or named scope predicate 昇格すれば
# xpass し strict=True が「修正済の xfail を外す」signal を吐く。reason 文字列
# は赤の *性質* (unrealized scope / value-degenerate drop) で分類し、後続作業者
# が何を直すべきか即わかる形で書く。

# 45 source × (fixture, case_factory)。順序は ``SOURCES`` レジストリ登録順を踏襲。
# 2 相運用は上の reason 定数群のコメント参照。
_ManifestEntry = tuple[ArticleSource, CaseFactory, pytest.MarkDecorator | None]
_MANIFEST: list[_ManifestEntry] = [
    (VentureBeatSource, _rss("venturebeat_rss.xml"), None),
    (TechCrunchSource, _rss("techcrunch_rss.xml"), None),
    (QuantumInsiderSource, _rss("quantum_insider_rss.xml"), None),
    (KrebsOnSecuritySource, _rss("krebs_on_security_rss.xml"), None),
    (SpaceflightNowSource, _rss("spaceflight_now_rss.xml"), None),
    (
        NASASource,
        _multi_feed_rss(
            {
                NASA_FEEDS[0]: "nasa_for_oracle_feed_a.xml",
                NASA_FEEDS[1]: "nasa_for_oracle_feed_b.xml",
            }
        ),
        None,
    ),
    (IEEESpectrumSource, _rss("ieee_spectrum_rss.xml"), None),
    (MicrosoftResearchSource, _rss("microsoft_research_rss.xml"), None),
    (ITmediaAISource, _rss("itmedia_ai_rss.xml"), None),
    (ITmediaNewsSource, _rss("itmedia_news_rss.xml"), None),
    (MONOistSource, _rss("monoist_rss.xml"), None),
    (EETimesJapanSource, _rss("eetimes_japan_rss.xml"), None),
    (EngadgetSource, _rss("engadget_rss.xml"), None),
    (FierceBiotechSource, _rss("fierce_biotech_rss.xml"), None),
    (JPCERTSource, _rss("jpcert_rss.xml"), None),
    (CleanTechnicaSource, _rss("cleantechnica_rss.xml"), None),
    (ElectrekSource, _rss("electrek_rss.xml"), None),
    (SpaceNewsSource, _rss("spacenews_rss.xml"), None),
    (TheRegisterSource, _rss("the_register_atom.xml"), None),
    (HackerNewsSource, _hn("hacker_news_hits.json"), None),
    (MEXTSource, _rss("mext_rdf.xml"), None),
    (MICSource, _rss("mic_rdf.xml"), None),
    (METISource, _rss("meti_atom.xml"), None),
    (AnthropicSource, _sitemap("anthropic_sitemap.xml"), None),
    (NISTSource, _rss("nist_rss.xml"), None),
    (NSFSource, _rss("nsf_rss.xml"), None),
    (CloudflareBlogSource, _rss("cloudflare_rss.xml"), None),
    (DeepMindSource, _rss("deepmind_rss.xml"), None),
    (ESAHubbleSource, _rss("esa_hubble_rss.xml"), None),
    (ESAWebbSource, _rss("esa_webb_rss.xml"), None),
    (OpenAISource, _rss("openai_rss.xml"), None),
    (HuggingFaceBlogSource, _rss("huggingface_blog_rss.xml"), None),
    (ELifeSource, _rss("elife_rss.xml"), None),
    (PLOSOneSource, _rss("plos_one_atom.xml"), None),
    (MetaAISource, _rss("meta_ai_rss.xml"), None),
    (
        CornellChronicleSource,
        _multi_feed_rss(
            {
                CORNELL_FEEDS[0]: "cornell_for_oracle_feed_a.xml",
                CORNELL_FEEDS[1]: "cornell_for_oracle_feed_b.xml",
            }
        ),
        None,
    ),
    (FrontiersAISource, _rss("frontiers_ai_rss.xml"), None),
    (FrontiersRoboticsAISource, _rss("frontiers_robotics_ai_rss.xml"), None),
    (FrontiersEnergyResearchSource, _rss("frontiers_energy_research_rss.xml"), None),
    (FrontiersMaterialsSource, _rss("frontiers_materials_rss.xml"), None),
    (
        ORNLSource,
        _html_listing(
            "ornl_listing_for_oracle.html",
            detail_link_xpath=ORNLSource.DETAIL_LINK_XPATH,
        ),
        None,
    ),
    (MDPIMaterialsSource, _crossref("mdpi_crossref.json"), None),
    (MDPIEnergiesSource, _crossref("mdpi_crossref.json"), None),
    (MDPISensorsSource, _crossref("mdpi_crossref.json"), None),
    (MDPINanomaterialsSource, _crossref("mdpi_crossref.json"), None),
]


# manifest と registry のドリフト検出 (新 source 追加時にここを更新し忘れたら fail)。
def test_manifest_covers_all_registered_sources() -> None:
    """``SOURCES`` レジストリ全 source が manifest に在る (drift 検出)。"""
    registered = {s for s in SOURCES.values()}
    manifested = {s for s, _, _ in _MANIFEST}
    assert registered == manifested, (
        f"manifest drift: registered - manifested = {registered - manifested}, "
        f"manifested - registered = {manifested - registered}"
    )


_PARAMS = [
    pytest.param(source, factory, id=str(source.name), marks=(mark,) if mark else ())
    for source, factory, mark in _MANIFEST
]


@pytest.mark.asyncio
@pytest.mark.parametrize(("source", "factory"), _PARAMS)
async def test_collect_yields_in_scope_count(
    source: ArticleSource, factory: CaseFactory
) -> None:
    """R7/R8: ``collect`` の yield 件数 == named scope を通った entry 件数。

    scope predicate を持たない source は entry 件数そのまま。これに合致しない
    source は写像内で per-item を裁いている (implicit drop) — Step N で
    scope predicate へ named 昇格するか drop 除去するかを triage する対象。

    fixture invariants: cap / dedup / fan-out が未発火な fixture を使うこと
    (本オラクル外の orchestration を写像 drop に誤帰属しないため)。
    """
    case = await factory()
    predicate = _SCOPE_PREDICATES.get(source)
    expected = (
        sum(1 for e in case.entries if predicate(e))
        if predicate is not None
        else len(case.entries)
    )
    try:
        yielded = [fa async for fa in source.collect(case.tools)]
    except ExternalFetchError as exc:  # pragma: no cover — fixture 不整合
        pytest.fail(f"unexpected fetch error from fake transport: {exc!r}")
    assert len(yielded) == expected, (
        f"R7/R8 violation for {source.name}: collect yielded {len(yielded)} "
        f"but {expected} entries were in scope (entries={len(case.entries)}, "
        f"predicate={'named' if predicate else 'none'}). "
        f"either implicit drop in mapping or unrealized scope predicate."
    )
