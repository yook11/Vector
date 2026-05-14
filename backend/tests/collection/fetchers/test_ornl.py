"""``ORNLNewsFetcher`` (HTML listing Pattern H) の不変条件テスト。

listing HTML の記事 link 抽出は本ソース固有のため XPath 結果も検証する。
それ以外の不変条件 (永続化可能性) は共通ヘルパーに委譲。
"""

from __future__ import annotations

from pathlib import Path

from app.collection.fetchers.ornl import ORNLNewsFetcher
from tests.collection.fetchers._invariant import (
    Passport,
    assert_at_least_one_passport,
    assert_passports_persistable,
)

_FIXTURE = Path(__file__).parent.parent.parent / "fixtures" / "ornl_listing.html"


def _passports() -> list[Passport]:
    fetcher = ORNLNewsFetcher()
    urls = ORNLNewsFetcher._parse_listing(_FIXTURE.read_bytes())
    seen: set[str] = set()
    items: list[Passport] = []
    for url in urls:
        if url in seen or not fetcher._url_matches(url):
            continue
        seen.add(url)
        converted = fetcher._convert_entry(url, 1)
        if converted is not None:
            items.append(converted)
    return items


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
    assert_at_least_one_passport(_passports())


def test_passports_satisfy_persistence_invariants() -> None:
    assert_passports_persistable(_passports())
