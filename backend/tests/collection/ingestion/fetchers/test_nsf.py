"""``NSFFetcher`` の単体テスト (Phase 3 PR 3-a)。

per-source 設計:
- RSS 2.0 (UTF-8) feed 形式を feedparser 標準経路で解釈
- ``<guid isPermaLink="false">`` を ``entry.id`` にマップ → guid に採用
- ``<dc:creator>`` は事実上 "NSF" 固定値 → metadata.author に格納
- per-entry の image_url / tags は未提供のため None / () 直書き
- PROVIDES = {language, guid, site_name}
- bytes 経由の feedparser.parse を想定
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
from app.collection.ingestion.fetchers.nsf import NSFFetcher

_FIXTURE = Path(__file__).parent.parent.parent.parent / "fixtures" / "nsf_rss.xml"

_SOURCE_ID = 1


def _entry(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "title": "Podcast: Photonic quantum chips promise fast future",
        "link": "https://www.nsf.gov/news/podcast-photonic-quantum-chips-promise-fast-future",
        "id": "https://www.nsf.gov/news/podcast-photonic-quantum-chips-promise-fast-future",
        "author": "NSF",
        "published_parsed": time.struct_time((2026, 4, 27, 20, 0, 15, 0, 0, 0)),
    }
    base.update(overrides)
    return base


class TestProvides:
    def test_provides_minimum_set(self) -> None:
        assert NSFFetcher.PROVIDES == frozenset({"language", "guid", "site_name"})

    def test_endpoint_is_rss(self) -> None:
        assert NSFFetcher.ENDPOINT_URL == "https://www.nsf.gov/rss/rss_www_news.xml"


class TestConvertEntry:
    def setup_method(self) -> None:
        self.fetcher = NSFFetcher()
        self.source_id = _SOURCE_ID

    def test_valid_entry_yields_pending(self) -> None:
        outcome = self.fetcher._convert_entry(_entry(), self.source_id, "en")
        assert isinstance(outcome, PendingHtmlFetch)
        assert outcome.title.startswith("Podcast: Photonic")

    def test_does_not_construct_body(self) -> None:
        # Pattern H: 本文は HTML 抽出 task の責務、Fetcher は触らない
        outcome = self.fetcher._convert_entry(_entry(), self.source_id, "en")
        assert isinstance(outcome, PendingHtmlFetch)
        assert not hasattr(outcome, "body")

    def test_empty_title_returns_failed(self) -> None:
        outcome = self.fetcher._convert_entry(_entry(title=""), self.source_id, "en")
        assert isinstance(outcome, Failed)
        assert outcome.reason.code == "title_missing"

    def test_whitespace_only_title_returns_failed(self) -> None:
        outcome = self.fetcher._convert_entry(
            _entry(title="   \n   "), self.source_id, "en"
        )
        assert isinstance(outcome, Failed)
        assert outcome.reason.code == "title_missing"

    def test_invalid_link_returns_failed(self) -> None:
        outcome = self.fetcher._convert_entry(
            _entry(link="not-a-url"), self.source_id, "en"
        )
        assert isinstance(outcome, Failed)
        assert outcome.reason.code == "extraction_empty"

    def test_published_parsed_yields_utc(self) -> None:
        outcome = self.fetcher._convert_entry(_entry(), self.source_id, "en")
        assert isinstance(outcome, PendingHtmlFetch)
        assert outcome.published_at_hint is not None
        assert (
            outcome.published_at_hint.value.tzinfo.utcoffset(None).total_seconds() == 0
        )

    def test_missing_pubdate_yields_pending_with_none_hint(self) -> None:
        # Pattern H 緩い品質ゲート: pubDate 欠落でも Failed しない
        entry = _entry()
        del entry["published_parsed"]
        outcome = self.fetcher._convert_entry(entry, self.source_id, "en")
        assert isinstance(outcome, PendingHtmlFetch)
        assert outcome.published_at_hint is None

    def test_metadata_author_extracted(self) -> None:
        outcome = self.fetcher._convert_entry(_entry(), self.source_id, "en")
        assert isinstance(outcome, PendingHtmlFetch)
        assert outcome.metadata.author == "NSF"

    def test_metadata_author_none_when_missing(self) -> None:
        entry = _entry()
        del entry["author"]
        outcome = self.fetcher._convert_entry(entry, self.source_id, "en")
        assert isinstance(outcome, PendingHtmlFetch)
        assert outcome.metadata.author is None

    def test_metadata_tags_hardcoded_empty(self) -> None:
        outcome = self.fetcher._convert_entry(_entry(), self.source_id, "en")
        assert isinstance(outcome, PendingHtmlFetch)
        assert outcome.metadata.tags == ()

    def test_metadata_image_url_hardcoded_none(self) -> None:
        outcome = self.fetcher._convert_entry(_entry(), self.source_id, "en")
        assert isinstance(outcome, PendingHtmlFetch)
        assert outcome.metadata.image_url is None

    def test_extracts_guid_from_id(self) -> None:
        outcome = self.fetcher._convert_entry(_entry(), self.source_id, "en")
        assert isinstance(outcome, PendingHtmlFetch)
        assert outcome.metadata.guid == (
            "https://www.nsf.gov/news/podcast-photonic-quantum-chips-promise-fast-future"
        )

    def test_language_passthrough_en(self) -> None:
        outcome = self.fetcher._convert_entry(_entry(), self.source_id, "en")
        assert isinstance(outcome, PendingHtmlFetch)
        assert outcome.metadata.language == "en"

    def test_site_name_hardcoded(self) -> None:
        outcome = self.fetcher._convert_entry(_entry(), self.source_id, "en")
        assert isinstance(outcome, PendingHtmlFetch)
        assert outcome.metadata.site_name == "NSF"


class TestFixtureParsing:
    def test_fixture_parses_as_rss(self) -> None:
        # bytes 経由で feedparser に渡し、RSS 2.0 として entries を 3 件取れること
        data = _FIXTURE.read_bytes()
        feed = feedparser.parse(data)
        assert len(feed.entries) == 3

    def test_fixture_first_entry_yields_pending(self) -> None:
        data = _FIXTURE.read_bytes()
        feed = feedparser.parse(data)
        fetcher = NSFFetcher()
        outcome = fetcher._convert_entry(feed.entries[0], _SOURCE_ID, "en")
        assert isinstance(outcome, PendingHtmlFetch)
        assert outcome.title.startswith("Podcast: Photonic")
        assert outcome.metadata.language == "en"
        assert outcome.metadata.site_name == "NSF"

    def test_fixture_first_entry_guid_set(self) -> None:
        data = _FIXTURE.read_bytes()
        feed = feedparser.parse(data)
        fetcher = NSFFetcher()
        outcome = fetcher._convert_entry(feed.entries[0], _SOURCE_ID, "en")
        assert isinstance(outcome, PendingHtmlFetch)
        assert outcome.metadata.guid is not None
        assert outcome.metadata.guid.startswith("https://www.nsf.gov/")

    def test_fixture_first_entry_has_published_hint(self) -> None:
        data = _FIXTURE.read_bytes()
        feed = feedparser.parse(data)
        fetcher = NSFFetcher()
        outcome = fetcher._convert_entry(feed.entries[0], _SOURCE_ID, "en")
        assert isinstance(outcome, PendingHtmlFetch)
        assert outcome.published_at_hint is not None

    def test_fixture_first_entry_has_author(self) -> None:
        data = _FIXTURE.read_bytes()
        feed = feedparser.parse(data)
        fetcher = NSFFetcher()
        outcome = fetcher._convert_entry(feed.entries[0], _SOURCE_ID, "en")
        assert isinstance(outcome, PendingHtmlFetch)
        assert outcome.metadata.author == "NSF"

    def test_fixture_third_entry_empty_title_yields_failed(self) -> None:
        data = _FIXTURE.read_bytes()
        feed = feedparser.parse(data)
        fetcher = NSFFetcher()
        outcome = fetcher._convert_entry(feed.entries[2], _SOURCE_ID, "en")
        assert isinstance(outcome, Failed)
        assert outcome.reason.code == "title_missing"
