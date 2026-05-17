"""non-RSS SourceAdapter machinery 7 本の invariant 一括検証 (P2)。

``test_rss_adapters_invariants.py`` と同思想で、各 machinery を fixture-backed
fake client を DI して組み立て、``ArticleSource`` でラップして
``ArticleFetcher`` 本番経路 (passport_builder) に通し、``_invariant.py`` の
4 assertion (passport 存在 / 型許容 / 主経路 / 永続化不変条件) を回す。

7 ケース内訳:

- Hacker News (Algolia HN Search API) — ``_FixtureHackerNewsApiClient``
- Anthropic (sitemap.xml) — ``_FixtureRawHttpClient`` + ``anthropic_sitemap.xml``
- ORNL (HTML listing) — ``_FixtureRawHttpClient`` + ``ornl_listing.html``
- MDPI Energies / Materials / Nanomaterials / Sensors (Crossref API) —
  ``_FixtureCrossrefApiClient`` + ``mdpi_crossref.json`` (P2: 4 ISSN は
  ``MDPICrossrefAdapter`` 汎用 machinery + 注入 ISSN で区別)

profile / origin は P1 と byte 不変: Anthropic=sitemap+HTML_TITLE /
ORNL=listing+HTML_TITLE (title 仮 ⇒ 全 ObservedArticle) / HN=api+DEFAULT /
MDPI=feed+DEFAULT (Pattern R ⇒ AnalyzableArticle)。
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from app.collection.domain.analyzable_article import AnalyzableArticle
from app.collection.domain.observed_article import ObservedArticle, ObservedOrigin
from app.collection.domain.source_completion_profile import (
    DEFAULT_PROFILE,
    HTML_TITLE_PROFILE,
    SourceCompletionProfile,
)
from app.collection.fetchers.anthropic import AnthropicAdapter
from app.collection.fetchers.article_fetcher import ArticleFetcher
from app.collection.fetchers.hacker_news import HackerNewsAdapter
from app.collection.fetchers.mdpi._common import MDPICrossrefAdapter
from app.collection.fetchers.ornl import ORNLAdapter
from app.collection.fetchers.tools.algolia_hn_client import HackerNewsApiClient
from app.collection.fetchers.tools.crossref_client import CrossrefApiClient
from app.collection.fetchers.tools.fetched_article import SourceAdapter
from app.collection.fetchers.tools.raw_http_client import RawHttpClient
from app.collection.sources.article_source import ArticleSource
from app.shared.value_objects.source_name import SourceName
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


def _source(
    adapter: SourceAdapter,
    *,
    name: str,
    origin: ObservedOrigin,
    profile: SourceCompletionProfile,
) -> ArticleSource:
    """fixture machinery を ``ArticleSource`` でラップ (本番経路と同じ profile)。"""
    return ArticleSource(
        name=SourceName(name),
        endpoint_url="https://example.test/source",
        observed_origin=origin,
        completion_profile=profile,
        adapter_factory=lambda: adapter,
    )


def _build_hn() -> ArticleSource:
    return _source(
        HackerNewsAdapter(
            source_name="Hacker News",
            client=_FixtureHackerNewsApiClient(_hn_hits()),
        ),
        name="Hacker News",
        origin=ObservedOrigin.api,
        profile=DEFAULT_PROFILE,
    )


def _build_anthropic() -> ArticleSource:
    payload = (_FIXTURES_DIR / "anthropic_sitemap.xml").read_bytes()
    return _source(
        AnthropicAdapter(
            endpoint_url="https://www.anthropic.com/sitemap.xml",
            source_name="Anthropic",
            client=_FixtureRawHttpClient(payload),
        ),
        name="Anthropic",
        origin=ObservedOrigin.sitemap,
        profile=HTML_TITLE_PROFILE,
    )


def _build_ornl() -> ArticleSource:
    payload = (_FIXTURES_DIR / "ornl_listing.html").read_bytes()
    return _source(
        ORNLAdapter(
            endpoint_url="https://www.ornl.gov/news",
            source_name="ORNL",
            client=_FixtureRawHttpClient(payload),
        ),
        name="ORNL",
        origin=ObservedOrigin.listing,
        profile=HTML_TITLE_PROFILE,
    )


def _build_mdpi(name: str, issn: str) -> ArticleSource:
    return _source(
        MDPICrossrefAdapter(
            source_name=name,
            issn=issn,
            client=_FixtureCrossrefApiClient(_mdpi_items()),
        ),
        name=name,
        origin=ObservedOrigin.feed,
        profile=DEFAULT_PROFILE,
    )


# (build_fn, allowed_types, must_include_types, label)
_CASES: list[tuple[Any, set[type], set[type], str]] = [
    (_build_hn, _H_BODY_DISTRUSTED, {ObservedArticle}, "HackerNews"),
    (_build_anthropic, _H_BODY_DISTRUSTED, {ObservedArticle}, "Anthropic"),
    (_build_ornl, _H_BODY_DISTRUSTED, {ObservedArticle}, "ORNL"),
    (
        lambda: _build_mdpi("MDPI Energies", "1996-1073"),
        _R_BODY_TRUSTED,
        {AnalyzableArticle},
        "MDPIEnergies",
    ),
    (
        lambda: _build_mdpi("MDPI Materials", "1996-1944"),
        _R_BODY_TRUSTED,
        {AnalyzableArticle},
        "MDPIMaterials",
    ),
    (
        lambda: _build_mdpi("MDPI Nanomaterials", "2079-4991"),
        _R_BODY_TRUSTED,
        {AnalyzableArticle},
        "MDPINanomaterials",
    ),
    (
        lambda: _build_mdpi("MDPI Sensors", "1424-8220"),
        _R_BODY_TRUSTED,
        {AnalyzableArticle},
        "MDPISensors",
    ),
]


async def _collect_passports(source: ArticleSource) -> list[Passport]:
    fetcher = ArticleFetcher(source)
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
