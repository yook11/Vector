"""``ESAHubbleFetcher`` (Djangoplicity Pattern H) の不変条件テスト。

base 振る舞いは ``test__common.py`` で検証、本ファイルは subclass の
identity (NAME/ENDPOINT_URL) と実 RSS fixture での pipeline 進行可能性に絞る。
"""

from __future__ import annotations

from pathlib import Path

import feedparser

from app.collection.fetchers.esa.hubble import ESAHubbleFetcher
from app.collection.fetchers.tools.rss_parser import normalize_entry
from tests.collection.fetchers._invariant import (
    Passport,
    assert_at_least_one_passport,
    assert_passports_persistable,
)

_FIXTURE = (
    Path(__file__).parent.parent.parent.parent / "fixtures" / "esa_hubble_rss.xml"
)


def _passports() -> list[Passport]:
    fetcher = ESAHubbleFetcher()
    feed = feedparser.parse(_FIXTURE.read_bytes())
    items: list[Passport] = []
    for e in feed.entries:
        converted = fetcher._convert_entry(normalize_entry(e), 1)
        if converted is not None:
            items.append(converted)
    return items


def test_identity_pinned() -> None:
    """NAME / ENDPOINT_URL は composition root の dispatch キーとして固定する。"""
    assert ESAHubbleFetcher.NAME == "ESA/Hubble"
    assert ESAHubbleFetcher.ENDPOINT_URL == "https://esahubble.org/news/feed/"


def test_fixture_yields_at_least_one_passport() -> None:
    assert_at_least_one_passport(_passports())


def test_passports_satisfy_persistence_invariants() -> None:
    assert_passports_persistable(_passports())
