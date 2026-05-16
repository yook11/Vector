"""non-RSS ``SourceAdapter`` 7 本の invariant 一括検証。

``test_rss_adapters_invariants.py`` と同思想で、各 Adapter を fixture-backed
fake client を DI して組み立て、``_invariant.py`` の 4 assertion (passport
存在 / 型許容 / 主経路 / 永続化不変条件) を回す。

7 ケース内訳:

- Hacker News (Algolia HN Search API) — ``_FixtureHackerNewsApiClient``
- Anthropic (sitemap.xml) — ``_FixtureRawHttpClient`` + ``anthropic_sitemap.xml``
- ORNL (HTML listing) — ``_FixtureRawHttpClient`` + ``ornl_listing.html``
- MDPI Energies / Materials / Nanomaterials / Sensors (Crossref API) —
  ``_FixtureCrossrefApiClient`` + ``mdpi_crossref.json``

fixture-backed fake は本物の HTTP client wrapper を継承し、本物の HTTP メソッド
が呼ばれないことで ``make_safe_async_client`` 経由の network I/O を完全に
排除する (P4 RSS の ``_FixtureRssParser`` と相同の戦略)。
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from app.collection.domain.analyzable_article import AnalyzableArticle
from app.collection.domain.incomplete_article import (
    IncompleteArticle,
)
from app.collection.fetchers.anthropic import AnthropicAdapter
from app.collection.fetchers.article_fetcher import ArticleFetcher
from app.collection.fetchers.hacker_news import HackerNewsAdapter
from app.collection.fetchers.mdpi.energies import MDPIEnergiesAdapter
from app.collection.fetchers.mdpi.materials import MDPIMaterialsAdapter
from app.collection.fetchers.mdpi.nanomaterials import MDPINanomaterialsAdapter
from app.collection.fetchers.mdpi.sensors import MDPISensorsAdapter
from app.collection.fetchers.ornl import ORNLAdapter
from app.collection.fetchers.tools.algolia_hn_client import HackerNewsApiClient
from app.collection.fetchers.tools.crossref_client import CrossrefApiClient
from app.collection.fetchers.tools.fetched_article import SourceAdapter
from app.collection.fetchers.tools.raw_http_client import RawHttpClient
from tests.collection.fetchers._invariant import (
    Passport,
    assert_at_least_one_passport,
    assert_passport_types_allowed,
    assert_passport_types_include,
    assert_passports_persistable,
)

_FIXTURES_DIR = Path(__file__).parent.parent.parent / "fixtures"

# 型集合表記は test_rss_adapters_invariants.py と同じ。
_R_BODY_TRUSTED = {AnalyzableArticle, IncompleteArticle}
_H_BODY_DISTRUSTED = {IncompleteArticle}


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


def _build_hn() -> HackerNewsAdapter:
    return HackerNewsAdapter(client=_FixtureHackerNewsApiClient(_hn_hits()))


def _build_anthropic() -> AnthropicAdapter:
    payload = (_FIXTURES_DIR / "anthropic_sitemap.xml").read_bytes()
    return AnthropicAdapter(client=_FixtureRawHttpClient(payload))


def _build_ornl() -> ORNLAdapter:
    payload = (_FIXTURES_DIR / "ornl_listing.html").read_bytes()
    return ORNLAdapter(client=_FixtureRawHttpClient(payload))


def _build_mdpi_energies() -> MDPIEnergiesAdapter:
    return MDPIEnergiesAdapter(client=_FixtureCrossrefApiClient(_mdpi_items()))


def _build_mdpi_materials() -> MDPIMaterialsAdapter:
    return MDPIMaterialsAdapter(client=_FixtureCrossrefApiClient(_mdpi_items()))


def _build_mdpi_nanomaterials() -> MDPINanomaterialsAdapter:
    return MDPINanomaterialsAdapter(client=_FixtureCrossrefApiClient(_mdpi_items()))


def _build_mdpi_sensors() -> MDPISensorsAdapter:
    return MDPISensorsAdapter(client=_FixtureCrossrefApiClient(_mdpi_items()))


AdapterBuilder = type(_build_hn)

# (build_fn, allowed_types, must_include_types, label)
_CASES: list[tuple[AdapterBuilder, set[type], set[type], str]] = [
    (_build_hn, _H_BODY_DISTRUSTED, {IncompleteArticle}, "HackerNewsAdapter"),
    (_build_anthropic, _H_BODY_DISTRUSTED, {IncompleteArticle}, "AnthropicAdapter"),
    (_build_ornl, _H_BODY_DISTRUSTED, {IncompleteArticle}, "ORNLAdapter"),
    (
        _build_mdpi_energies,
        _R_BODY_TRUSTED,
        {AnalyzableArticle},
        "MDPIEnergiesAdapter",
    ),
    (
        _build_mdpi_materials,
        _R_BODY_TRUSTED,
        {AnalyzableArticle},
        "MDPIMaterialsAdapter",
    ),
    (
        _build_mdpi_nanomaterials,
        _R_BODY_TRUSTED,
        {AnalyzableArticle},
        "MDPINanomaterialsAdapter",
    ),
    (
        _build_mdpi_sensors,
        _R_BODY_TRUSTED,
        {AnalyzableArticle},
        "MDPISensorsAdapter",
    ),
]


async def _collect_passports(adapter: SourceAdapter) -> list[Passport]:
    fetcher = ArticleFetcher(adapter)
    items: AsyncIterator[Passport] = fetcher.fetch(source_id=1)
    return [item async for item in items]


@pytest.fixture(params=_CASES, ids=lambda c: c[3])
async def case(
    request: pytest.FixtureRequest,
) -> tuple[list[Passport], set[type], set[type]]:
    build, allowed, must_include, _label = request.param
    passports = await _collect_passports(build())
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
