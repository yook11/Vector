"""``ITmediaNewsFetcher`` の単体テスト (Phase 1c-C)。

per-source 設計:
- title 接頭辞除去**なし** (ITmedia AI+ と異なる、本ソースは `[...]` を持たない)
- author / tags / image_url / guid は ``None`` / ``()`` 直書き
- PROVIDES から ``guid`` を除外
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
from app.collection.ingestion.fetchers.itmedia_news import ITmediaNewsFetcher
from app.models.news_source import NewsSource

_FIXTURE = (
    Path(__file__).parent.parent.parent.parent / "fixtures" / "itmedia_news_rss.xml"
)


def _source(source_id: int = 1, name: str = "ITmedia NEWS") -> NewsSource:
    s = MagicMock(spec=NewsSource)
    s.id = source_id
    s.name = name
    s.endpoint_url = "https://rss.itmedia.co.jp/rss/2.0/news_bursts.xml"
    return s


def _entry(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "title": "NTT、6G 向け次世代光通信技術を発表",
        "link": "https://www.itmedia.co.jp/news/articles/2604/30/news001.html",
        "summary": "NTT は 6G に向けた次世代光通信技術を発表した。",
        "published_parsed": time.struct_time((2026, 4, 30, 14, 30, 0, 0, 0, 0)),
    }
    base.update(overrides)
    return base


class TestProvides:
    def test_provides_minimum_set(self) -> None:
        assert ITmediaNewsFetcher.PROVIDES == frozenset({"language", "site_name"})


class TestConvertEntry:
    def setup_method(self) -> None:
        self.fetcher = ITmediaNewsFetcher()
        self.source = _source()

    def test_valid_entry_yields_pending(self) -> None:
        outcome = self.fetcher._convert_entry(_entry(), self.source, "ja")
        assert isinstance(outcome, PendingHtmlFetch)
        assert outcome.title.startswith("NTT")

    def test_does_not_strip_bracket_prefix(self) -> None:
        # ITmedia AI+ と異なり、本ソースは接頭辞除去をしない
        outcome = self.fetcher._convert_entry(
            _entry(title="[Foo] Bar"), self.source, "ja"
        )
        assert isinstance(outcome, PendingHtmlFetch)
        assert outcome.title == "[Foo] Bar"

    def test_empty_title_returns_failed(self) -> None:
        outcome = self.fetcher._convert_entry(_entry(title=""), self.source, "ja")
        assert isinstance(outcome, Failed)
        assert outcome.reason.code == "title_missing"

    def test_invalid_link_returns_failed(self) -> None:
        outcome = self.fetcher._convert_entry(
            _entry(link="not-a-url"), self.source, "ja"
        )
        assert isinstance(outcome, Failed)
        assert outcome.reason.code == "extraction_empty"

    def test_metadata_minimum(self) -> None:
        outcome = self.fetcher._convert_entry(_entry(), self.source, "ja")
        assert isinstance(outcome, PendingHtmlFetch)
        assert outcome.metadata.author is None
        assert outcome.metadata.tags == ()
        assert outcome.metadata.image_url is None
        assert outcome.metadata.guid is None
        assert outcome.metadata.language == "ja"
        assert outcome.metadata.site_name == "ITmedia NEWS"


class TestFixtureParsing:
    def test_fixture_first_entry_yields_pending(self) -> None:
        text = _FIXTURE.read_text(encoding="utf-8")
        feed = feedparser.parse(text)
        fetcher = ITmediaNewsFetcher()
        outcome = fetcher._convert_entry(feed.entries[0], _source(), "ja")
        assert isinstance(outcome, PendingHtmlFetch)
        assert outcome.title.startswith("NTT")
        assert outcome.metadata.language == "ja"
        assert outcome.metadata.site_name == "ITmedia NEWS"
