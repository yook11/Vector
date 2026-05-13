"""``ESAWebbFetcher`` (Djangoplicity Pattern H) の不変条件テスト。"""

from __future__ import annotations

from pathlib import Path

import feedparser

from app.collection.fetchers.esa.webb import ESAWebbFetcher
from app.collection.fetchers.outcome import FetchOutcome
from tests.collection.fetchers._invariant import (
    assert_at_least_one_passport,
    assert_metadata_audit_safe,
    assert_passports_persistable,
    assert_provides_contract,
)

_FIXTURE = Path(__file__).parent.parent.parent.parent / "fixtures" / "esa_webb_rss.xml"


def _outcomes() -> list[FetchOutcome]:
    fetcher = ESAWebbFetcher()
    feed = feedparser.parse(_FIXTURE.read_bytes())
    return [fetcher._convert_entry(e, 1, "en") for e in feed.entries]


def test_identity_pinned() -> None:
    assert ESAWebbFetcher.NAME == "ESA/Webb"
    assert ESAWebbFetcher.ENDPOINT_URL == "https://esawebb.org/news/feed/"


def test_fixture_yields_at_least_one_passport() -> None:
    assert_at_least_one_passport(_outcomes())


def test_passports_satisfy_persistence_invariants() -> None:
    assert_passports_persistable(_outcomes())


def test_provides_contract_holds() -> None:
    assert_provides_contract(_outcomes(), ESAWebbFetcher.PROVIDES)


def test_metadata_audit_safe() -> None:
    assert_metadata_audit_safe(_outcomes())
