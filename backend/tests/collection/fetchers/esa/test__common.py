"""``BaseDjangoplicityFetcher`` の振る舞い不変条件テスト (Phase 3 PR 3-b)。

base 自体はインスタンス化しないため、最小 ClassVar を持った dummy subclass
で振る舞いを検証する。
"""

from __future__ import annotations

import time
from typing import Any, ClassVar

from app.collection.fetchers.esa._common import (
    BaseDjangoplicityFetcher,
    _normalize_language,
)
from app.collection.fetchers.outcome import (
    FetchedEntry,
    FetchOutcome,
    SourceFetchFailed,
)
from app.collection.incomplete_article.domain.incomplete_article import (
    IncompleteArticle,
)
from tests.collection.fetchers._invariant import (
    assert_at_least_one_passport,
    assert_metadata_audit_safe,
    assert_passports_persistable,
    assert_provides_contract,
)


class _DummyFetcher(BaseDjangoplicityFetcher):
    NAME: ClassVar[str] = "Dummy"
    ENDPOINT_URL: ClassVar[str] = "https://dummy.example.com/feed/"
    SITE_NAME: ClassVar[str] = "Dummy Site"
    AUTHOR: ClassVar[str] = "Dummy Org"


def _entry(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "title": "Photo Release: Dummy Title",
        "link": "https://dummy.example.com/news/abc/",
        "id": "https://dummy.example.com/news/abc/",
        "published_parsed": time.struct_time((2026, 4, 20, 16, 0, 0, 0, 0, 0)),
    }
    base.update(overrides)
    return base


def test_normalize_language_default_when_none() -> None:
    assert _normalize_language(None, default="en") == "en"


def test_normalize_language_underscore_normalized() -> None:
    assert _normalize_language("en_US", default="en") == "en-US"


def test_normalize_language_truncated_to_20_chars() -> None:
    assert len(_normalize_language("a" * 50, default="en")) == 20


def _outcomes() -> list[FetchOutcome]:
    fetcher = _DummyFetcher()
    return [fetcher._convert_entry(_entry(), 1, "en")]


def test_valid_entry_yields_pending_passport() -> None:
    assert_at_least_one_passport(_outcomes())


def test_passport_satisfies_persistence_invariants() -> None:
    assert_passports_persistable(_outcomes())


def test_provides_contract_holds() -> None:
    assert_provides_contract(_outcomes(), _DummyFetcher.PROVIDES)


def test_metadata_audit_safe() -> None:
    assert_metadata_audit_safe(_outcomes())


def test_empty_title_returns_failed_not_corrupt_passport() -> None:
    fetcher = _DummyFetcher()
    outcome = fetcher._convert_entry(_entry(title=""), 1, "en")
    assert isinstance(outcome, SourceFetchFailed)
    assert outcome.reason.code == "title_missing"


def test_invalid_link_returns_failed() -> None:
    fetcher = _DummyFetcher()
    outcome = fetcher._convert_entry(_entry(link="not-a-url"), 1, "en")
    assert isinstance(outcome, SourceFetchFailed)
    assert outcome.reason.code == "extraction_empty"


def test_missing_pubdate_does_not_block_pattern_h() -> None:
    """Pattern H: published_at は HTML 抽出に委ねる (hint=None でも passport)。"""
    fetcher = _DummyFetcher()
    entry = _entry()
    del entry["published_parsed"]
    outcome = fetcher._convert_entry(entry, 1, "en")
    assert isinstance(outcome, FetchedEntry)
    assert isinstance(outcome.item, IncompleteArticle)
    assert outcome.item.published_at_hint is None
