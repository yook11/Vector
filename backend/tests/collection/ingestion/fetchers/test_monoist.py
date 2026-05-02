"""``MONOistFetcher`` の単体テスト (Phase 1c-C)。

per-source 設計: ITmedia NEWS と同形 (site_name のみ差分)。
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import feedparser

from app.collection.ingestion.domain.fetched_article import (
    Failed,
    PendingHtmlFetch,
)
from app.collection.ingestion.fetchers.monoist import MONOistFetcher

_FIXTURE = Path(__file__).parent.parent.parent.parent / "fixtures" / "monoist_rss.xml"


_SOURCE_ID = 1


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
        self.source_id = _SOURCE_ID

    def test_valid_entry_yields_pending(self) -> None:
        outcome = self.fetcher._convert_entry(_entry(), self.source_id, "ja")
        assert isinstance(outcome, PendingHtmlFetch)
        assert outcome.title.startswith("トヨタ")

    def test_empty_title_returns_failed(self) -> None:
        outcome = self.fetcher._convert_entry(_entry(title=""), self.source_id, "ja")
        assert isinstance(outcome, Failed)
        assert outcome.reason.code == "title_missing"

    def test_metadata_minimum(self) -> None:
        outcome = self.fetcher._convert_entry(_entry(), self.source_id, "ja")
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
        outcome = fetcher._convert_entry(feed.entries[0], _SOURCE_ID, "ja")
        assert isinstance(outcome, PendingHtmlFetch)
        assert outcome.title.startswith("トヨタ")
        assert outcome.metadata.site_name == "MONOist"
