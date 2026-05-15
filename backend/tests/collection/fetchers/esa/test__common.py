"""``BaseDjangoplicityFetcher`` の振る舞い不変条件テスト。

base 自体はインスタンス化しないため、最小 ClassVar を持った dummy subclass
で振る舞いを検証する。
"""

from __future__ import annotations

import time
from typing import Any, ClassVar

from app.collection.fetchers.esa._common import BaseDjangoplicityFetcher
from app.collection.fetchers.tools.rss_parser import RssEntry, normalize_entry
from app.collection.incomplete_article.domain.incomplete_article import (
    IncompleteArticle,
)
from tests.collection.fetchers._invariant import (
    Passport,
    assert_at_least_one_passport,
    assert_passports_persistable,
)


class _DummyFetcher(BaseDjangoplicityFetcher):
    NAME: ClassVar[str] = "Dummy"
    ENDPOINT_URL: ClassVar[str] = "https://dummy.example.com/feed/"


def _entry(**overrides: Any) -> RssEntry:
    base: dict[str, Any] = {
        "title": "Photo Release: Dummy Title",
        "link": "https://dummy.example.com/news/abc/",
        "id": "https://dummy.example.com/news/abc/",
        "published_parsed": time.struct_time((2026, 4, 20, 16, 0, 0, 0, 0, 0)),
    }
    base.update(overrides)
    return normalize_entry(base)


def _passports() -> list[Passport]:
    fetcher = _DummyFetcher()
    converted = fetcher._convert_entry(_entry(), 1)
    return [converted] if converted is not None else []


def test_valid_entry_yields_pending_passport() -> None:
    assert_at_least_one_passport(_passports())


def test_passport_satisfies_persistence_invariants() -> None:
    assert_passports_persistable(_passports())


def test_empty_title_dropped() -> None:
    fetcher = _DummyFetcher()
    assert fetcher._convert_entry(_entry(title=""), 1) is None


def test_invalid_link_dropped() -> None:
    fetcher = _DummyFetcher()
    assert fetcher._convert_entry(_entry(link="not-a-url"), 1) is None


def test_missing_pubdate_does_not_block_pattern_h() -> None:
    """Pattern H: published_at は HTML 抽出に委ねる (hint=None でも passport)。"""
    fetcher = _DummyFetcher()
    entry_dict: dict[str, Any] = {
        "title": "Photo Release: Dummy Title",
        "link": "https://dummy.example.com/news/abc/",
        "id": "https://dummy.example.com/news/abc/",
    }
    converted = fetcher._convert_entry(normalize_entry(entry_dict), 1)
    assert isinstance(converted, IncompleteArticle)
    assert converted.published_at_hint is None
