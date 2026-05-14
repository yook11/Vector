"""``BaseFrontiersFetcher`` (Pattern R) の振る舞い不変条件テスト (Phase 3 PR 3-c-3)。

base 自体は dummy ClassVar を埋めた concrete subclass で検証する。
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import feedparser

from app.collection.fetchers.frontiers._common import BaseFrontiersFetcher
from tests.collection.fetchers._invariant import (
    Passport,
    assert_at_least_one_passport,
    assert_passports_persistable,
)

_FIXTURE_AI = (
    Path(__file__).parent.parent.parent.parent / "fixtures" / "frontiers_ai_rss.xml"
)


class _ConcreteFrontiersFetcher(BaseFrontiersFetcher):
    NAME = "Frontiers in Test Journal"
    ENDPOINT_URL = "https://www.frontiersin.org/journals/test-journal/rss"
    JOURNAL_NAME = "Frontiers in Test Journal"


def _entry(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "title": "A study on neural architecture search for time series",
        "link": "https://www.frontiersin.org/articles/10.3389/frai.2026.1234567",
        "id": "https://www.frontiersin.org/articles/10.3389/frai.2026.1234567",
        "summary": (
            "This is a long enough abstract describing our novel approach "
            "to neural architecture search for time series forecasting. "
            "We propose a method that achieves state-of-the-art performance."
        ),
        "published_parsed": time.struct_time((2026, 5, 4, 0, 0, 0, 0, 0, 0)),
        "author": "Jane Researcher",
    }
    base.update(overrides)
    return base


def _passports_from_fixture() -> list[Passport]:
    fetcher = _ConcreteFrontiersFetcher()
    feed = feedparser.parse(_FIXTURE_AI.read_bytes())
    items: list[Passport] = []
    for e in feed.entries:
        converted = fetcher._convert_entry(e, 1)
        if converted is not None:
            items.append(converted)
    return items


def test_fixture_yields_at_least_one_passport() -> None:
    assert_at_least_one_passport(_passports_from_fixture())


def test_passports_satisfy_persistence_invariants() -> None:
    assert_passports_persistable(_passports_from_fixture())


def test_short_body_dropped() -> None:
    """Pattern R: description が短い editorial 等は drop。"""
    fetcher = _ConcreteFrontiersFetcher()
    assert fetcher._convert_entry(_entry(summary="brief."), 1) is None


def test_missing_pubdate_dropped() -> None:
    """Pattern R: published_at は HTML 補完がないため必須。"""
    fetcher = _ConcreteFrontiersFetcher()
    entry = _entry()
    del entry["published_parsed"]
    assert fetcher._convert_entry(entry, 1) is None


def test_invalid_link_dropped() -> None:
    fetcher = _ConcreteFrontiersFetcher()
    assert fetcher._convert_entry(_entry(link="not-a-url"), 1) is None
