"""``EETimesJapanFetcher`` の単体テスト (Phase 1c-C)。

per-source 設計: ITmedia NEWS と同形 (site_name のみ差分)。
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import feedparser

from app.collection.ingestion.domain.fetched_article import (
    Failed,
    PendingHtmlFetch,
)
from app.collection.ingestion.fetchers.eetimes_japan import EETimesJapanFetcher
from app.models.news_source import NewsSource

_FIXTURE = (
    Path(__file__).parent.parent.parent.parent / "fixtures" / "eetimes_japan_rss.xml"
)


def _source(source_id: int = 1, name: str = "EE Times Japan") -> NewsSource:
    s = MagicMock(spec=NewsSource)
    s.id = source_id
    s.name = name
    s.endpoint_url = "https://rss.itmedia.co.jp/rss/2.0/eetimes.xml"
    return s


def _entry(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "title": "TSMC、3nm プロセスの量産開始を発表",
        "link": "https://eetimes.itmedia.co.jp/ee/articles/2604/30/news001.html",
        "summary": "TSMC は 3nm プロセスの量産開始を発表した。",
        "published_parsed": time.struct_time((2026, 4, 30, 14, 0, 0, 0, 0, 0)),
    }
    base.update(overrides)
    return base


class TestProvides:
    def test_provides_minimum_set(self) -> None:
        assert EETimesJapanFetcher.PROVIDES == frozenset({"language", "site_name"})


class TestConvertEntry:
    def setup_method(self) -> None:
        self.fetcher = EETimesJapanFetcher()
        self.source = _source()

    def test_valid_entry_yields_pending(self) -> None:
        outcome = self.fetcher._convert_entry(_entry(), self.source, "ja")
        assert isinstance(outcome, PendingHtmlFetch)
        assert outcome.title.startswith("TSMC")

    def test_empty_title_returns_failed(self) -> None:
        outcome = self.fetcher._convert_entry(_entry(title=""), self.source, "ja")
        assert isinstance(outcome, Failed)
        assert outcome.reason.code == "title_missing"

    def test_metadata_minimum(self) -> None:
        outcome = self.fetcher._convert_entry(_entry(), self.source, "ja")
        assert isinstance(outcome, PendingHtmlFetch)
        assert outcome.metadata.author is None
        assert outcome.metadata.tags == ()
        assert outcome.metadata.image_url is None
        assert outcome.metadata.guid is None
        assert outcome.metadata.site_name == "EE Times Japan"


class TestFixtureParsing:
    def test_fixture_first_entry_yields_pending(self) -> None:
        text = _FIXTURE.read_text(encoding="utf-8")
        feed = feedparser.parse(text)
        fetcher = EETimesJapanFetcher()
        outcome = fetcher._convert_entry(feed.entries[0], _source(), "ja")
        assert isinstance(outcome, PendingHtmlFetch)
        assert outcome.title.startswith("TSMC")
        assert outcome.metadata.site_name == "EE Times Japan"
