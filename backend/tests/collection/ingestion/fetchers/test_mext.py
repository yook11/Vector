"""``MEXTFetcher`` の単体テスト (Phase 3 PR 3-h-1)。

per-source 設計:
- RDF (RSS 1.0) feed 形式を feedparser 標準経路で解釈 (UTF-8)
- ``<item rdf:about="URL">`` を ``entry.id`` にマップ → guid に採用
- ``<dc:date>`` ISO 8601 → feedparser 標準経路で ``published_parsed`` を populate
- author / tags / image_url は per-entry で未提供のため None / () 直書き
- PROVIDES = {language, guid, site_name} (JPCERT/CC と同形)
- bytes 経由の feedparser.parse を想定 (XML 宣言の encoding を sniff)
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
from app.collection.ingestion.fetchers.mext import MEXTFetcher

_FIXTURE = Path(__file__).parent.parent.parent.parent / "fixtures" / "mext_rdf.xml"

_SOURCE_ID = 1


def _entry(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "title": "令和8年度科学研究費助成事業の交付内定について(第1次)",
        "link": "https://www.mext.go.jp/b_menu/houdou/2026/05/01.htm",
        "id": "https://www.mext.go.jp/b_menu/houdou/2026/05/01.htm",
        "published_parsed": time.struct_time((2026, 5, 1, 1, 0, 0, 0, 0, 0)),
    }
    base.update(overrides)
    return base


class TestProvides:
    def test_provides_minimum_set(self) -> None:
        assert MEXTFetcher.PROVIDES == frozenset({"language", "guid", "site_name"})


class TestConvertEntry:
    def setup_method(self) -> None:
        self.fetcher = MEXTFetcher()
        self.source_id = _SOURCE_ID

    def test_valid_entry_yields_pending(self) -> None:
        outcome = self.fetcher._convert_entry(_entry(), self.source_id, "ja")
        assert isinstance(outcome, PendingHtmlFetch)
        assert outcome.title.startswith("令和8年度")

    def test_does_not_construct_body(self) -> None:
        # Pattern H: 本文は HTML 抽出 task の責務、Fetcher は触らない
        outcome = self.fetcher._convert_entry(_entry(), self.source_id, "ja")
        assert isinstance(outcome, PendingHtmlFetch)
        assert not hasattr(outcome, "body")

    def test_empty_title_returns_failed(self) -> None:
        outcome = self.fetcher._convert_entry(_entry(title=""), self.source_id, "ja")
        assert isinstance(outcome, Failed)
        assert outcome.reason.code == "title_missing"

    def test_whitespace_only_title_returns_failed(self) -> None:
        outcome = self.fetcher._convert_entry(
            _entry(title="   \n   "), self.source_id, "ja"
        )
        assert isinstance(outcome, Failed)
        assert outcome.reason.code == "title_missing"

    def test_invalid_link_returns_failed(self) -> None:
        outcome = self.fetcher._convert_entry(
            _entry(link="not-a-url"), self.source_id, "ja"
        )
        assert isinstance(outcome, Failed)
        assert outcome.reason.code == "extraction_empty"

    def test_published_parsed_yields_utc(self) -> None:
        outcome = self.fetcher._convert_entry(_entry(), self.source_id, "ja")
        assert isinstance(outcome, PendingHtmlFetch)
        assert outcome.published_at_hint is not None
        assert (
            outcome.published_at_hint.value.tzinfo.utcoffset(None).total_seconds() == 0
        )

    def test_missing_pubdate_yields_pending_with_none_hint(self) -> None:
        # Pattern H 緩い品質ゲート: pubDate 欠落でも Failed しない
        entry = _entry()
        del entry["published_parsed"]
        outcome = self.fetcher._convert_entry(entry, self.source_id, "ja")
        assert isinstance(outcome, PendingHtmlFetch)
        assert outcome.published_at_hint is None

    def test_metadata_author_is_none(self) -> None:
        outcome = self.fetcher._convert_entry(_entry(), self.source_id, "ja")
        assert isinstance(outcome, PendingHtmlFetch)
        assert outcome.metadata.author is None

    def test_metadata_tags_hardcoded_empty(self) -> None:
        outcome = self.fetcher._convert_entry(_entry(), self.source_id, "ja")
        assert isinstance(outcome, PendingHtmlFetch)
        assert outcome.metadata.tags == ()

    def test_metadata_image_url_hardcoded_none(self) -> None:
        outcome = self.fetcher._convert_entry(_entry(), self.source_id, "ja")
        assert isinstance(outcome, PendingHtmlFetch)
        assert outcome.metadata.image_url is None

    def test_extracts_guid_from_id(self) -> None:
        outcome = self.fetcher._convert_entry(_entry(), self.source_id, "ja")
        assert isinstance(outcome, PendingHtmlFetch)
        assert outcome.metadata.guid == (
            "https://www.mext.go.jp/b_menu/houdou/2026/05/01.htm"
        )

    def test_language_passthrough_ja(self) -> None:
        outcome = self.fetcher._convert_entry(_entry(), self.source_id, "ja")
        assert isinstance(outcome, PendingHtmlFetch)
        assert outcome.metadata.language == "ja"

    def test_site_name_hardcoded(self) -> None:
        outcome = self.fetcher._convert_entry(_entry(), self.source_id, "ja")
        assert isinstance(outcome, PendingHtmlFetch)
        assert outcome.metadata.site_name == "MEXT"


class TestFixtureParsing:
    def test_fixture_parses_as_rdf(self) -> None:
        # bytes 経由で feedparser に渡し、RDF として entries を 3 件取れること
        data = _FIXTURE.read_bytes()
        feed = feedparser.parse(data)
        assert len(feed.entries) == 3

    def test_fixture_first_entry_yields_pending(self) -> None:
        data = _FIXTURE.read_bytes()
        feed = feedparser.parse(data)
        fetcher = MEXTFetcher()
        outcome = fetcher._convert_entry(feed.entries[0], _SOURCE_ID, "ja")
        assert isinstance(outcome, PendingHtmlFetch)
        assert outcome.title.startswith("令和8年度")
        assert outcome.metadata.language == "ja"
        assert outcome.metadata.site_name == "MEXT"

    def test_fixture_first_entry_guid_is_rdf_about_url(self) -> None:
        data = _FIXTURE.read_bytes()
        feed = feedparser.parse(data)
        fetcher = MEXTFetcher()
        outcome = fetcher._convert_entry(feed.entries[0], _SOURCE_ID, "ja")
        assert isinstance(outcome, PendingHtmlFetch)
        assert outcome.metadata.guid is not None
        assert outcome.metadata.guid.startswith("https://www.mext.go.jp/")

    def test_fixture_first_entry_has_published_hint(self) -> None:
        data = _FIXTURE.read_bytes()
        feed = feedparser.parse(data)
        fetcher = MEXTFetcher()
        outcome = fetcher._convert_entry(feed.entries[0], _SOURCE_ID, "ja")
        assert isinstance(outcome, PendingHtmlFetch)
        assert outcome.published_at_hint is not None

    def test_fixture_third_entry_empty_title_yields_failed(self) -> None:
        data = _FIXTURE.read_bytes()
        feed = feedparser.parse(data)
        fetcher = MEXTFetcher()
        outcome = fetcher._convert_entry(feed.entries[2], _SOURCE_ID, "ja")
        assert isinstance(outcome, Failed)
        assert outcome.reason.code == "title_missing"
