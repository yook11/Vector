"""SourceAdapter 経路の RSS 共通不変条件テスト。

``ArticleFetcher(XxxAdapter())`` を本番経路と同じ ``passport_builder`` 経由で
通すときに、各 source が以下の不変条件を満たすことを fixture ベースで検証する:

- 実 fixture から少なくとも 1 件は永続化 passport を yield する
- yield された passport の型は ``allowed_types`` 集合に属する
- ``must_include_types`` の各型を最低 1 件含む (主経路の挙動を固定)
- yield された passport は永続化不変条件 (Stage 2 を通せば articles に
  永続化できる) を満たす

旧 Fetcher 用の ``test_rss_fetchers_invariants.py`` と並存させ、Adapter 経路
専用の構造的保証を独立に維持する。Adapter は ``RssParser.fetch()`` を呼ぶため、
``_FixtureRssParser`` で feedparser + ``normalize_entry`` を経由した
``RssEntry`` 列を返し、ネットワーク I/O を完全に排除する。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import feedparser
import pytest

from app.collection.article.domain.article import ReadyForArticle
from app.collection.fetchers.article_fetcher import ArticleFetcher
from app.collection.fetchers.cleantechnica import CleanTechnicaAdapter
from app.collection.fetchers.deepmind import DeepMindAdapter
from app.collection.fetchers.eetimes_japan import EETimesJapanAdapter
from app.collection.fetchers.electrek import ElectrekAdapter
from app.collection.fetchers.engadget import EngadgetAdapter
from app.collection.fetchers.huggingface import HuggingFaceBlogAdapter
from app.collection.fetchers.itmedia_ai import ITmediaAIAdapter
from app.collection.fetchers.itmedia_news import ITmediaNewsAdapter
from app.collection.fetchers.jpcert import JPCERTAdapter
from app.collection.fetchers.meti import METIAdapter
from app.collection.fetchers.mext import MEXTAdapter
from app.collection.fetchers.mic import MICAdapter
from app.collection.fetchers.monoist import MONOistAdapter
from app.collection.fetchers.nist import NISTAdapter
from app.collection.fetchers.nsf import NSFAdapter
from app.collection.fetchers.openai import OpenAIAdapter
from app.collection.fetchers.spacenews import SpaceNewsAdapter
from app.collection.fetchers.techcrunch import TechCrunchAdapter
from app.collection.fetchers.tools.fetched_article import SourceAdapter
from app.collection.fetchers.tools.rss_parser import RssEntry, normalize_entry
from app.collection.fetchers.venturebeat import VentureBeatAdapter
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

# AdapterFactory = parser を受け取り Adapter を返す callable。
# テスト本体で ``_FixtureRssParser(fixture)`` を生成して factory に渡す。
AdapterFactory = type[SourceAdapter]


class _FixtureRssParser:
    """``RssParser`` の構造的 fake。fixture を feedparser で読み、
    ``normalize_entry`` を通して本番経路と同じ ``RssEntry`` を返す。

    ``parse_mode`` は受け取って無視する (fixture は静的バイナリなので encoding
    差異を再現する必要がない)。本物の ``RssParser.fetch`` と同じ kw シグネチャ
    を満たすため、将来引数追加にも ``**_`` で耐える。
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


# 旧 invariant test と同じ Ready/Incomplete 集合表記。
_R_BODY_TRUSTED = {ReadyForArticle, IncompleteArticle}
_H_BODY_DISTRUSTED = {IncompleteArticle}

# (adapter_class, fixture_filename, allowed_types, must_include_types)
# Adapter は parser を __init__ で受けるため、test 本体で
# ``adapter_class(parser=_FixtureRssParser(fixture))`` を組む。
_CASES: list[tuple[AdapterFactory, str, set[type], set[type]]] = [
    # P2 で導入済 (代表 2 本)
    (VentureBeatAdapter, "venturebeat_rss.xml", _R_BODY_TRUSTED, {ReadyForArticle}),
    (
        VentureBeatAdapter,
        "venturebeat_teaser_rss.xml",
        {IncompleteArticle},
        {IncompleteArticle},
    ),
    (TechCrunchAdapter, "techcrunch_rss.xml", _H_BODY_DISTRUSTED, {IncompleteArticle}),
    # P3a (Pattern H stand-alone, 17 本) — body 不信用、Incomplete 経路に固定
    (
        CleanTechnicaAdapter,
        "cleantechnica_rss.xml",
        _H_BODY_DISTRUSTED,
        {IncompleteArticle},
    ),
    (DeepMindAdapter, "deepmind_rss.xml", _H_BODY_DISTRUSTED, {IncompleteArticle}),
    (
        EETimesJapanAdapter,
        "eetimes_japan_rss.xml",
        _H_BODY_DISTRUSTED,
        {IncompleteArticle},
    ),
    (ElectrekAdapter, "electrek_rss.xml", _H_BODY_DISTRUSTED, {IncompleteArticle}),
    (EngadgetAdapter, "engadget_rss.xml", _H_BODY_DISTRUSTED, {IncompleteArticle}),
    (
        HuggingFaceBlogAdapter,
        "huggingface_blog_rss.xml",
        _H_BODY_DISTRUSTED,
        {IncompleteArticle},
    ),
    (ITmediaAIAdapter, "itmedia_ai_rss.xml", _H_BODY_DISTRUSTED, {IncompleteArticle}),
    (
        ITmediaNewsAdapter,
        "itmedia_news_rss.xml",
        _H_BODY_DISTRUSTED,
        {IncompleteArticle},
    ),
    (JPCERTAdapter, "jpcert_rss.xml", _H_BODY_DISTRUSTED, {IncompleteArticle}),
    (METIAdapter, "meti_atom.xml", _H_BODY_DISTRUSTED, {IncompleteArticle}),
    (MEXTAdapter, "mext_rdf.xml", _H_BODY_DISTRUSTED, {IncompleteArticle}),
    (MICAdapter, "mic_rdf.xml", _H_BODY_DISTRUSTED, {IncompleteArticle}),
    (MONOistAdapter, "monoist_rss.xml", _H_BODY_DISTRUSTED, {IncompleteArticle}),
    (NISTAdapter, "nist_rss.xml", _H_BODY_DISTRUSTED, {IncompleteArticle}),
    (NSFAdapter, "nsf_rss.xml", _H_BODY_DISTRUSTED, {IncompleteArticle}),
    (OpenAIAdapter, "openai_rss.xml", _H_BODY_DISTRUSTED, {IncompleteArticle}),
    (SpaceNewsAdapter, "spacenews_rss.xml", _H_BODY_DISTRUSTED, {IncompleteArticle}),
]


async def _collect_passports(
    adapter_cls: AdapterFactory, fixture_filename: str
) -> list[Passport]:
    """``ArticleFetcher(adapter)`` 経由で fixture を流し passport を集める。"""
    parser = _FixtureRssParser(fixture_filename)
    adapter = adapter_cls(parser=parser)  # type: ignore[call-arg]
    fetcher = ArticleFetcher(adapter)
    items: AsyncIterator[Passport] = fetcher.fetch(source_id=1)
    return [item async for item in items]


@pytest.fixture(params=_CASES, ids=lambda c: f"{c[0].__name__}-{c[1]}")
async def case(
    request: pytest.FixtureRequest,
) -> tuple[list[Passport], set[type], set[type]]:
    adapter_cls, fixture_name, allowed, must_include = request.param
    passports = await _collect_passports(adapter_cls, fixture_name)
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
