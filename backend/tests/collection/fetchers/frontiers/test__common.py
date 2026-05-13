"""``BaseFrontiersFetcher`` (Pattern R) の振る舞い不変条件テスト (Phase 3 PR 3-c-3)。

base 自体は dummy ClassVar を埋めた concrete subclass で検証する。DOI 抽出
の正規表現は subclass 共通の重要ロジックなので個別に網羅する。
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import feedparser

from app.collection.fetchers.frontiers._common import (
    BaseFrontiersFetcher,
    _extract_doi,
)
from app.collection.ingestion.domain.fetched_article import (
    Failed,
    FetchOutcome,
)
from tests.collection.fetchers._invariant import (
    assert_at_least_one_passport,
    assert_metadata_audit_safe,
    assert_passports_persistable,
    assert_provides_contract,
)

_FIXTURE_AI = (
    Path(__file__).parent.parent.parent.parent
    / "fixtures"
    / "frontiers_ai_rss.xml"
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


def test_doi_extractor_handles_known_journal_prefixes() -> None:
    """4 journal の DOI URL を正規表現で抽出できる (subclass 共通の不変条件)。"""
    for prefix in ("frai", "frobt", "fenrg", "fmats"):
        link = f"https://www.frontiersin.org/articles/10.3389/{prefix}.2026.1767330"
        assert _extract_doi(link) == f"10.3389/{prefix}.2026.1767330"


def test_doi_extractor_returns_none_for_unrelated_url() -> None:
    assert _extract_doi("https://example.com/no-doi") is None
    assert _extract_doi(None) is None


def _outcomes_from_fixture() -> list[FetchOutcome]:
    fetcher = _ConcreteFrontiersFetcher()
    feed = feedparser.parse(_FIXTURE_AI.read_bytes())
    return [fetcher._convert_entry(e, 1, "en") for e in feed.entries]


def test_fixture_yields_at_least_one_passport() -> None:
    assert_at_least_one_passport(_outcomes_from_fixture())


def test_passports_satisfy_persistence_invariants() -> None:
    assert_passports_persistable(_outcomes_from_fixture())


def test_provides_contract_holds() -> None:
    assert_provides_contract(
        _outcomes_from_fixture(), _ConcreteFrontiersFetcher.PROVIDES
    )


def test_metadata_audit_safe() -> None:
    assert_metadata_audit_safe(_outcomes_from_fixture())


def test_short_body_dropped_as_failed() -> None:
    """Pattern R: description が短い editorial 等は body_too_short で drop。"""
    fetcher = _ConcreteFrontiersFetcher()
    outcome = fetcher._convert_entry(_entry(summary="brief."), 1, "en")
    assert isinstance(outcome, Failed)
    assert outcome.reason.code == "body_too_short"


def test_missing_pubdate_dropped_as_failed() -> None:
    """Pattern R: published_at は HTML 補完がないため必須。"""
    fetcher = _ConcreteFrontiersFetcher()
    entry = _entry()
    del entry["published_parsed"]
    outcome = fetcher._convert_entry(entry, 1, "en")
    assert isinstance(outcome, Failed)
    assert outcome.reason.code == "published_at_missing"


def test_invalid_link_dropped_as_failed() -> None:
    fetcher = _ConcreteFrontiersFetcher()
    outcome = fetcher._convert_entry(_entry(link="not-a-url"), 1, "en")
    assert isinstance(outcome, Failed)
    assert outcome.reason.code == "extraction_empty"
