"""``ORNLNewsFetcher`` (HTML listing Pattern H) の不変条件テスト。

listing HTML の記事 link 抽出は本ソース固有のため XPath 結果も検証する。
それ以外の不変条件 (永続化可能性 / PROVIDES / audit) は共通ヘルパーに委譲。
"""

from __future__ import annotations

from pathlib import Path

from app.collection.fetchers.ornl import ORNLNewsFetcher
from app.collection.fetchers.outcome import FetchOutcome
from tests.collection.fetchers._invariant import (
    assert_at_least_one_passport,
    assert_metadata_audit_safe,
    assert_passports_persistable,
    assert_provides_contract,
)

_FIXTURE = Path(__file__).parent.parent.parent / "fixtures" / "ornl_listing.html"


def _outcomes() -> list[FetchOutcome]:
    fetcher = ORNLNewsFetcher()
    urls = ORNLNewsFetcher._parse_listing(_FIXTURE.read_bytes())
    seen: set[str] = set()
    outcomes: list[FetchOutcome] = []
    for url in urls:
        if url in seen or not fetcher._url_matches(url):
            continue
        seen.add(url)
        outcomes.append(fetcher._convert_entry(url, 1))
    return outcomes


def test_listing_xpath_extracts_only_news_links() -> None:
    """listing HTML から ``/news/`` 配下の link のみが抽出されること。"""
    urls = ORNLNewsFetcher._parse_listing(_FIXTURE.read_bytes())
    assert urls
    assert all(url.startswith("https://www.ornl.gov/news/") for url in urls)


def test_excluded_paths_drop_category_landings() -> None:
    """category landing は yield されない (記事ページのみが下流に流れる)。"""
    fetcher = ORNLNewsFetcher()
    for category_path in fetcher.EXCLUDED_PATHS:
        assert not fetcher._url_matches(f"https://www.ornl.gov{category_path}")


def test_fixture_yields_at_least_one_passport() -> None:
    assert_at_least_one_passport(_outcomes())


def test_passports_satisfy_persistence_invariants() -> None:
    assert_passports_persistable(_outcomes())


def test_provides_contract_holds() -> None:
    assert_provides_contract(_outcomes(), ORNLNewsFetcher.PROVIDES)


def test_metadata_audit_safe() -> None:
    assert_metadata_audit_safe(_outcomes())
