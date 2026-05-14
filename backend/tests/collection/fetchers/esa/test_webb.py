"""``ESAWebbFetcher`` (Djangoplicity Pattern H) の不変条件テスト。"""

from __future__ import annotations

from pathlib import Path

import feedparser

from app.collection.fetchers.esa.webb import ESAWebbFetcher
from tests.collection.fetchers._invariant import (
    Passport,
    assert_at_least_one_passport,
    assert_passports_persistable,
)

_FIXTURE = Path(__file__).parent.parent.parent.parent / "fixtures" / "esa_webb_rss.xml"


def _passports() -> list[Passport]:
    fetcher = ESAWebbFetcher()
    feed = feedparser.parse(_FIXTURE.read_bytes())
    items: list[Passport] = []
    for e in feed.entries:
        converted = fetcher._convert_entry(e, 1)
        if converted is not None:
            items.append(converted)
    return items


def test_identity_pinned() -> None:
    assert ESAWebbFetcher.NAME == "ESA/Webb"
    assert ESAWebbFetcher.ENDPOINT_URL == "https://esawebb.org/news/feed/"


def test_fixture_yields_at_least_one_passport() -> None:
    assert_at_least_one_passport(_passports())


def test_passports_satisfy_persistence_invariants() -> None:
    assert_passports_persistable(_passports())
