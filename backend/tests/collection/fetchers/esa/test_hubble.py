"""``ESAHubbleFetcher`` (Djangoplicity Pattern H) の不変条件テスト。

base 振る舞いは ``test__common.py`` で検証、本ファイルは subclass の
identity (NAME/ENDPOINT_URL/SITE_NAME) と実 RSS fixture での pipeline
進行可能性 + PROVIDES 契約に絞る。
"""

from __future__ import annotations

from pathlib import Path

import feedparser

from app.collection.fetchers.esa.hubble import ESAHubbleFetcher
from app.collection.ingestion.domain.fetched_article import FetchOutcome
from tests.collection.fetchers._invariant import (
    assert_at_least_one_passport,
    assert_metadata_audit_safe,
    assert_passports_persistable,
    assert_provides_contract,
)

_FIXTURE = (
    Path(__file__).parent.parent.parent.parent
    / "fixtures"
    / "esa_hubble_rss.xml"
)


def _outcomes() -> list[FetchOutcome]:
    fetcher = ESAHubbleFetcher()
    feed = feedparser.parse(_FIXTURE.read_bytes())
    return [fetcher._convert_entry(e, 1, "en") for e in feed.entries]


def test_identity_pinned() -> None:
    """NAME / ENDPOINT_URL は composition root の dispatch キーとして固定する。"""
    assert ESAHubbleFetcher.NAME == "ESA/Hubble"
    assert ESAHubbleFetcher.ENDPOINT_URL == "https://esahubble.org/news/feed/"


def test_fixture_yields_at_least_one_passport() -> None:
    assert_at_least_one_passport(_outcomes())


def test_passports_satisfy_persistence_invariants() -> None:
    assert_passports_persistable(_outcomes())


def test_provides_contract_holds() -> None:
    assert_provides_contract(_outcomes(), ESAHubbleFetcher.PROVIDES)


def test_metadata_audit_safe() -> None:
    assert_metadata_audit_safe(_outcomes())
