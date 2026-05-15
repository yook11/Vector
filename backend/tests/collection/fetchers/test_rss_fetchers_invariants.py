"""RSS / Atom / RDF 経路の Fetcher 共通不変条件テスト。

各 Fetcher の per-source 実装ごとに以下の不変条件を検証する:

- 実 fixture から少なくとも 1 件は永続化 passport を yield する
- yield された passport の型は ``allowed_types`` 集合に属する
- ``must_include_types`` の各型を最低 1 件含む (主経路の挙動を固定)
- yield された passport は永続化不変条件 (Stage 2 を通せば articles に
  永続化できる) を満たす

passport builder 移行後は同じ Fetcher でも entry ごとに Ready / Incomplete が
振れうるため、型集合検証を 2 段階 (allowed / must_include) で行うことで
fallback の混入を許容しつつ主経路の死を検知する。

ソース固有の挙動 (encoding 処理、独自フィルタ等) は各 Fetcher 単体テストで補う。
本テストは「pipeline 進行可能性」と「主経路 / 副経路の型契約」のゲートに集中する。
"""

from __future__ import annotations

from pathlib import Path

import feedparser
import pytest

from app.collection.article.domain.article import ReadyForArticle
from app.collection.fetchers.cleantechnica import CleanTechnicaFetcher
from app.collection.fetchers.cloudflare import CloudflareBlogFetcher
from app.collection.fetchers.cornell import CornellChronicleFetcher
from app.collection.fetchers.deepmind import DeepMindFetcher
from app.collection.fetchers.eetimes_japan import EETimesJapanFetcher
from app.collection.fetchers.electrek import ElectrekFetcher
from app.collection.fetchers.elife import ELifeFetcher
from app.collection.fetchers.engadget import EngadgetFetcher
from app.collection.fetchers.fierce_biotech import FierceBiotechFetcher
from app.collection.fetchers.huggingface import HuggingFaceBlogFetcher
from app.collection.fetchers.ieee_spectrum import IEEESpectrumFetcher
from app.collection.fetchers.itmedia_ai import ITmediaAIFetcher
from app.collection.fetchers.itmedia_news import ITmediaNewsFetcher
from app.collection.fetchers.jpcert import JPCERTFetcher
from app.collection.fetchers.krebs_on_security import KrebsOnSecurityFetcher
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
from app.collection.fetchers.plos_one import PLOSOneFetcher
from app.collection.fetchers.protocol import Fetcher
from app.collection.fetchers.quantum_insider import QuantumInsiderFetcher
from app.collection.fetchers.spaceflight_now import SpaceflightNowFetcher
from app.collection.fetchers.spacenews import SpaceNewsFetcher
from app.collection.fetchers.techcrunch import TechCrunchFetcher
from app.collection.fetchers.the_register import TheRegisterFetcher
from app.collection.fetchers.tools.rss_parser import normalize_entry
from app.collection.fetchers.venturebeat import VentureBeatFetcher
from app.collection.incomplete_article.domain.incomplete_article import (
    IncompleteArticle,
)
from tests.collection.fetchers._invariant import (
    Passport,
    assert_at_least_one_passport,
    assert_passport_types_allowed,
    assert_passport_types_include,
    assert_passports_persistable,
)

_FIXTURES_DIR = Path(__file__).parent.parent.parent / "fixtures"

# passport builder 移行で「source = Pattern R/H 固定」は消えたが、各 fixture
# について「主経路 (must_include) で yield される型」「許容 (allowed) 型集合」
# を 4-tuple で固定する。
#
# - body 候補が RSS にある source (旧 Pattern R 系): full fixture では Ready
#   主経路、ただし body 短い entry が混じれば Incomplete fallback を許容
# - body 候補を持たない source (旧 Pattern H 系 / TC, OpenAI 等): Incomplete
#   経路に固定 (allowed も Incomplete のみ)
# - VB teaser fixture: 全 entry が teaser のときの Ready→Incomplete fallback
#   を構造的に固定する
_R_BODY_TRUSTED = {ReadyForArticle, IncompleteArticle}
_H_BODY_DISTRUSTED = {IncompleteArticle}

# (fetcher_class, fixture_filename, allowed_types, must_include_types)
_CASES: list[tuple[type[Fetcher], str, set[type], set[type]]] = [
    # body 不信用 (旧 Pattern H) — Incomplete のみ
    (
        CleanTechnicaFetcher,
        "cleantechnica_rss.xml",
        _H_BODY_DISTRUSTED,
        {IncompleteArticle},
    ),
    (
        CornellChronicleFetcher,
        "cornell_rss.xml",
        _H_BODY_DISTRUSTED,
        {IncompleteArticle},
    ),
    (DeepMindFetcher, "deepmind_rss.xml", _H_BODY_DISTRUSTED, {IncompleteArticle}),
    (
        EETimesJapanFetcher,
        "eetimes_japan_rss.xml",
        _H_BODY_DISTRUSTED,
        {IncompleteArticle},
    ),
    (ElectrekFetcher, "electrek_rss.xml", _H_BODY_DISTRUSTED, {IncompleteArticle}),
    (EngadgetFetcher, "engadget_rss.xml", _H_BODY_DISTRUSTED, {IncompleteArticle}),
    (
        FierceBiotechFetcher,
        "fierce_biotech_rss.xml",
        _H_BODY_DISTRUSTED,
        {IncompleteArticle},
    ),
    (
        HuggingFaceBlogFetcher,
        "huggingface_blog_rss.xml",
        _H_BODY_DISTRUSTED,
        {IncompleteArticle},
    ),
    (ITmediaAIFetcher, "itmedia_ai_rss.xml", _H_BODY_DISTRUSTED, {IncompleteArticle}),
    (
        ITmediaNewsFetcher,
        "itmedia_news_rss.xml",
        _H_BODY_DISTRUSTED,
        {IncompleteArticle},
    ),
    (JPCERTFetcher, "jpcert_rss.xml", _H_BODY_DISTRUSTED, {IncompleteArticle}),
    (METIFetcher, "meti_atom.xml", _H_BODY_DISTRUSTED, {IncompleteArticle}),
    (MEXTFetcher, "mext_rdf.xml", _H_BODY_DISTRUSTED, {IncompleteArticle}),
    (MICFetcher, "mic_rdf.xml", _H_BODY_DISTRUSTED, {IncompleteArticle}),
    (MONOistFetcher, "monoist_rss.xml", _H_BODY_DISTRUSTED, {IncompleteArticle}),
    (NISTFetcher, "nist_rss.xml", _H_BODY_DISTRUSTED, {IncompleteArticle}),
    (NSFFetcher, "nsf_rss.xml", _H_BODY_DISTRUSTED, {IncompleteArticle}),
    (OpenAIFetcher, "openai_rss.xml", _H_BODY_DISTRUSTED, {IncompleteArticle}),
    (SpaceNewsFetcher, "spacenews_rss.xml", _H_BODY_DISTRUSTED, {IncompleteArticle}),
    (TechCrunchFetcher, "techcrunch_rss.xml", _H_BODY_DISTRUSTED, {IncompleteArticle}),
    # PR2 で builder 経由に切替予定の固有挙動 fetcher — 現状は per-source 内で
    # 直接 Ready / Incomplete を生成しているため移行前の出力型に合わせる
    (
        TheRegisterFetcher,
        "the_register_atom.xml",
        _H_BODY_DISTRUSTED,
        {IncompleteArticle},
    ),
    (MetaAIFetcher, "meta_ai_rss.xml", _R_BODY_TRUSTED, {ReadyForArticle}),
    (NASAFetcher, "nasa_rss.xml", _R_BODY_TRUSTED, {ReadyForArticle}),
    # body 信用 (旧 Pattern R) — full fixture は Ready 主経路、teaser entry が
    # 混じれば Incomplete fallback を許容
    (CloudflareBlogFetcher, "cloudflare_rss.xml", _R_BODY_TRUSTED, {ReadyForArticle}),
    (ELifeFetcher, "elife_rss.xml", _R_BODY_TRUSTED, {ReadyForArticle}),
    (IEEESpectrumFetcher, "ieee_spectrum_rss.xml", _R_BODY_TRUSTED, {ReadyForArticle}),
    (
        KrebsOnSecurityFetcher,
        "krebs_on_security_rss.xml",
        _R_BODY_TRUSTED,
        {ReadyForArticle},
    ),
    (
        MicrosoftResearchFetcher,
        "microsoft_research_rss.xml",
        _R_BODY_TRUSTED,
        {ReadyForArticle},
    ),
    (PLOSOneFetcher, "plos_one_atom.xml", _R_BODY_TRUSTED, {ReadyForArticle}),
    (
        QuantumInsiderFetcher,
        "quantum_insider_rss.xml",
        _R_BODY_TRUSTED,
        {ReadyForArticle},
    ),
    (
        SpaceflightNowFetcher,
        "spaceflight_now_rss.xml",
        _R_BODY_TRUSTED,
        {ReadyForArticle},
    ),
    (VentureBeatFetcher, "venturebeat_rss.xml", _R_BODY_TRUSTED, {ReadyForArticle}),
    # VB teaser-only fixture: builder の Ready→Incomplete fallback 経路を固定。
    # 全 entry が teaser (body < 50) なので Incomplete に落ちる構造的保証。
    (
        VentureBeatFetcher,
        "venturebeat_teaser_rss.xml",
        {IncompleteArticle},
        {IncompleteArticle},
    ),
]


@pytest.fixture(params=_CASES, ids=lambda c: f"{c[0].__name__}-{c[1]}")
def case(
    request: pytest.FixtureRequest,
) -> tuple[list[Passport], set[type], set[type]]:
    cls, fixture_name, allowed, must_include = request.param
    feed = feedparser.parse((_FIXTURES_DIR / fixture_name).read_bytes())
    fetcher = cls()
    items: list[Passport] = []
    for raw in feed.entries:
        converted = fetcher._convert_entry(normalize_entry(raw), 1)
        if converted is not None:
            assert isinstance(converted, ReadyForArticle | IncompleteArticle)
            items.append(converted)
    return items, allowed, must_include


def test_fixture_yields_at_least_one_passport(
    case: tuple[list[Passport], set[type], set[type]],
) -> None:
    passports, _, _ = case
    assert_at_least_one_passport(passports)


def test_passport_types_within_allowed_set(
    case: tuple[list[Passport], set[type], set[type]],
) -> None:
    passports, allowed, _ = case
    assert_passport_types_allowed(passports, allowed=allowed)


def test_passport_main_route_types_present(
    case: tuple[list[Passport], set[type], set[type]],
) -> None:
    passports, _, must_include = case
    assert_passport_types_include(passports, must_include=must_include)


def test_passports_satisfy_persistence_invariants(
    case: tuple[list[Passport], set[type], set[type]],
) -> None:
    passports, _, _ = case
    assert_passports_persistable(passports)
