"""``MONOistFetcher`` の単体テスト (Phase 1c-C)。

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
from app.collection.ingestion.fetchers.monoist import MONOistFetcher
from app.models.news_source import NewsSource

_FIXTURE = Path(__file__).parent.parent.parent.parent / "fixtures" / "monoist_rss.xml"


def _source(source_id: int = 1, name: str = "MONOist") -> NewsSource:
    s = MagicMock(spec=NewsSource)
    s.id = source_id
    s.name = name
    s.endpoint_url = "https://rss.itmedia.co.jp/rss/2.0/monoist.xml"
    return s


def _entry(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "title": "トヨタ、全固体電池の量産ラインを 2027 年に稼働",
        "link": "https://monoist.itmedia.co.jp/mn/articles/2604/30/news001.html",
        "summary": (
            "トヨタは全固体電池の量産ラインを 2027 年に稼働させる方針を発表した。"
        ),
        "published_parsed": time.struct_time((2026, 4, 30, 13, 0, 0, 0, 0, 0)),
    }
    base.update(overrides)
    return base


class TestProvides:
    def test_provides_minimum_set(self) -> None:
        assert MONOistFetcher.PROVIDES == frozenset({"language", "site_name"})


class TestConvertEntry:
    def setup_method(self) -> None:
        self.fetcher = MONOistFetcher()
        self.source = _source()

    def test_valid_entry_yields_pending(self) -> None:
        outcome = self.fetcher._convert_entry(_entry(), self.source, "ja")
        assert isinstance(outcome, PendingHtmlFetch)
        assert outcome.title.startswith("トヨタ")

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
        assert outcome.metadata.site_name == "MONOist"


class TestFixtureParsing:
    def test_fixture_first_entry_yields_pending(self) -> None:
        text = _FIXTURE.read_text(encoding="utf-8")
        feed = feedparser.parse(text)
        fetcher = MONOistFetcher()
        outcome = fetcher._convert_entry(feed.entries[0], _source(), "ja")
        assert isinstance(outcome, PendingHtmlFetch)
        assert outcome.title.startswith("トヨタ")
        assert outcome.metadata.site_name == "MONOist"
