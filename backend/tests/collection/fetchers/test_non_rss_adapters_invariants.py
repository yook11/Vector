"""non-RSS ``XxxSource`` 7 本の invariant 一括検証 (P2-D)。

``test_rss_adapters_invariants.py`` と同思想で、``FetchTools`` に fixture-backed
fake client を注入し、Source クラスオブジェクトを ``ArticleFetcher`` 本番経路
(passport_builder) に通し、``_invariant.py`` の 4 assertion (passport 存在 /
型許容 / 主経路 / 永続化不変条件) を回す。

7 ケース内訳:

- Hacker News (Algolia HN Search API) — ``_FixtureHackerNewsApiClient``
- Anthropic (sitemap.xml) — ``_FixtureRawHttpClient`` + ``anthropic_sitemap.xml``
- ORNL (HTML listing) — ``_FixtureRawHttpClient`` + ``ornl_listing.html``
- MDPI Energies / Materials / Nanomaterials / Sensors (Crossref API) —
  ``_FixtureCrossrefApiClient`` + ``mdpi_crossref.json`` (P2-D: 4 ISSN は
  独立した ``MDPIXxxSource`` クラスで区別。共通処理 ``mdpi_items`` を共有)

profile / origin は P1 と byte 不変 (Source クラスの ``ClassVar`` 直読み):
Anthropic=sitemap+HTML_TITLE / ORNL=listing+HTML_TITLE (title 仮 ⇒ 全
ObservedArticle) / HN=api+DEFAULT / MDPI=feed+DEFAULT (Pattern R ⇒
AnalyzableArticle)。
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import pytest

from app.collection.domain.analyzable_article import AnalyzableArticle
from app.collection.domain.observed_article import ObservedArticle
from app.collection.source_fetch.article_fetcher import ArticleFetcher
from app.collection.source_fetch.tools.algolia_hn_client import HackerNewsApiClient
from app.collection.source_fetch.tools.crossref_client import CrossrefApiClient
from app.collection.source_fetch.tools.fetch_tools import FetchTools
from app.collection.source_fetch.tools.raw_http_client import RawHttpClient
from app.collection.sources.article_source import ArticleSource
from app.collection.sources.definitions.anthropic import AnthropicSource
from app.collection.sources.definitions.hacker_news import HackerNewsSource
from app.collection.sources.definitions.mdpi.sources import (
    MDPIEnergiesSource,
    MDPIMaterialsSource,
    MDPINanomaterialsSource,
    MDPISensorsSource,
)
from app.collection.sources.definitions.ornl import ORNLSource
from tests.collection.fetchers._fixture_tools import fixture_tools
from tests.collection.fetchers._invariant import (
    Passport,
    assert_at_least_one_passport,
    assert_passport_types_allowed,
    assert_passport_types_include,
    assert_passports_persistable,
)

_FIXTURES_DIR = Path(__file__).parent.parent.parent / "fixtures"

# 型集合表記は test_rss_adapters_invariants.py と同じ。
_R_BODY_TRUSTED = {AnalyzableArticle, ObservedArticle}
_H_BODY_DISTRUSTED = {ObservedArticle}


class _FixtureRawHttpClient(RawHttpClient):
    """``RawHttpClient`` の構造的 fake。fixture バイト列を直接返す。"""

    def __init__(self, payload: bytes) -> None:
        # 親 ``__init__`` は呼ばない (network 系の attr は使わないため)。
        self._payload = payload

    async def fetch(self, *, url: str, source_name: str) -> bytes:  # noqa: ARG002
        return self._payload


class _FixtureHackerNewsApiClient(HackerNewsApiClient):
    """``HackerNewsApiClient`` の構造的 fake。fixture hits を直接返す。"""

    def __init__(self, hits: list[dict[str, Any]]) -> None:
        self._hits = hits

    async def search_recent_stories(
        self,
        *,
        source_name: str,  # noqa: ARG002
        min_points: int,  # noqa: ARG002
        window_seconds: int,  # noqa: ARG002
        hits_per_page: int,  # noqa: ARG002
    ) -> list[dict[str, Any]]:
        return self._hits


class _FixtureCrossrefApiClient(CrossrefApiClient):
    """``CrossrefApiClient`` の構造的 fake。fixture items を直接返す。"""

    def __init__(self, items: list[dict[str, Any]]) -> None:
        self._items = items

    async def works(
        self,
        *,
        source_name: str,  # noqa: ARG002
        issn: str,  # noqa: ARG002
        from_pub_date: str,  # noqa: ARG002
        rows: int,  # noqa: ARG002
    ) -> list[dict[str, Any]]:
        return self._items


def _hn_hits() -> list[dict[str, Any]]:
    raw = json.loads((_FIXTURES_DIR / "hacker_news_hits.json").read_text())
    return list(raw["hits"])


def _mdpi_items() -> list[dict[str, Any]]:
    raw = json.loads((_FIXTURES_DIR / "mdpi_crossref.json").read_text())
    return list(raw["message"]["items"])


def _hn_tools() -> FetchTools:
    return fixture_tools(hacker_news=_FixtureHackerNewsApiClient(_hn_hits()))


def _raw_tools(fixture_filename: str) -> Callable[[], FetchTools]:
    payload = (_FIXTURES_DIR / fixture_filename).read_bytes()
    return lambda: fixture_tools(raw=_FixtureRawHttpClient(payload))


def _mdpi_tools() -> FetchTools:
    return fixture_tools(crossref=_FixtureCrossrefApiClient(_mdpi_items()))


# (label, SourceClass, tools_factory, allowed_types, must_include_types)
_Case = tuple[str, ArticleSource, Callable[[], FetchTools], set[type], set[type]]
_CASES: list[_Case] = [
    ("HackerNews", HackerNewsSource, _hn_tools, _H_BODY_DISTRUSTED, {ObservedArticle}),
    (
        "Anthropic",
        AnthropicSource,
        _raw_tools("anthropic_sitemap.xml"),
        _H_BODY_DISTRUSTED,
        {ObservedArticle},
    ),
    (
        "ORNL",
        ORNLSource,
        _raw_tools("ornl_listing.html"),
        _H_BODY_DISTRUSTED,
        {ObservedArticle},
    ),
    (
        "MDPIEnergies",
        MDPIEnergiesSource,
        _mdpi_tools,
        _R_BODY_TRUSTED,
        {AnalyzableArticle},
    ),
    (
        "MDPIMaterials",
        MDPIMaterialsSource,
        _mdpi_tools,
        _R_BODY_TRUSTED,
        {AnalyzableArticle},
    ),
    (
        "MDPINanomaterials",
        MDPINanomaterialsSource,
        _mdpi_tools,
        _R_BODY_TRUSTED,
        {AnalyzableArticle},
    ),
    (
        "MDPISensors",
        MDPISensorsSource,
        _mdpi_tools,
        _R_BODY_TRUSTED,
        {AnalyzableArticle},
    ),
]


async def _collect_passports(
    source: ArticleSource, tools: FetchTools
) -> list[Passport]:
    fetcher = ArticleFetcher(source, tools=tools)
    items: AsyncIterator[Passport] = fetcher.fetch(source_id=1)
    return [item async for item in items]


@pytest.fixture(params=_CASES, ids=lambda c: c[0])
async def case(
    request: pytest.FixtureRequest,
) -> tuple[list[Passport], set[type], set[type]]:
    _label, source, tools_factory, allowed, must_include = request.param
    passports = await _collect_passports(source, tools_factory())
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
