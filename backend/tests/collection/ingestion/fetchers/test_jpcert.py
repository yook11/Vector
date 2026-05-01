"""``JPCERTFetcher`` の単体テスト (Phase 1c-E、Pattern H 最終)。

per-source 設計:
- RDF (RSS 1.0) feed 形式を feedparser 標準経路で解釈
- ``<item rdf:about="URL">`` を ``entry.id`` にマップ → guid に採用
- 多行 + インデント空白を含む title を ``_strip_html`` で 1 行化
- "注意喚起:" 接頭辞は **保持** (コンテンツ本体、navigational noise ではない)
- ``<dc:date>`` ISO 8601 → feedparser path のみ (FB のような strptime fallback なし)
- author / tags / image_url は per-entry で未提供のため None / () 直書き
- PROVIDES = {language, guid, site_name} (FB / Engadget と同形)
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
from app.collection.ingestion.fetchers.jpcert import JPCERTFetcher
from app.models.news_source import NewsSource

_FIXTURE = Path(__file__).parent.parent.parent.parent / "fixtures" / "jpcert_rss.xml"


def _source(source_id: int = 1, name: str = "JPCERT/CC") -> NewsSource:
    s = MagicMock(spec=NewsSource)
    s.id = source_id
    s.name = name
    s.endpoint_url = "https://www.jpcert.or.jp/rss/jpcert.rdf"
    return s


def _entry(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "title": "注意喚起: Cisco ASAおよびFTDにおける複数の脆弱性に関する注意喚起",
        "link": "https://www.jpcert.or.jp/at/2026/at260021.html",
        "id": "https://www.jpcert.or.jp/at/2026/at260021.html",
        "published_parsed": time.struct_time((2026, 4, 30, 1, 47, 0, 0, 0, 0)),
    }
    base.update(overrides)
    return base


class TestProvides:
    def test_provides_minimum_set(self) -> None:
        assert JPCERTFetcher.PROVIDES == frozenset({"language", "guid", "site_name"})


class TestConvertEntry:
    def setup_method(self) -> None:
        self.fetcher = JPCERTFetcher()
        self.source = _source()

    def test_valid_entry_yields_pending(self) -> None:
        outcome = self.fetcher._convert_entry(_entry(), self.source, "ja")
        assert isinstance(outcome, PendingHtmlFetch)
        assert outcome.title.startswith("注意喚起:")

    def test_does_not_construct_body(self) -> None:
        outcome = self.fetcher._convert_entry(_entry(), self.source, "ja")
        assert isinstance(outcome, PendingHtmlFetch)
        assert not hasattr(outcome, "body")

    def test_title_normalizes_multiline_whitespace(self) -> None:
        # 実 RSS で観察される多行 + インデント形式
        multiline = (
            "注意喚起: Cisco ASAおよびFTDにおける複数の脆弱性\n"
            "    （CVE-2026-12345、CVE-2026-12346）に関する注意喚起 (更新)"
        )
        outcome = self.fetcher._convert_entry(
            _entry(title=multiline), self.source, "ja"
        )
        assert isinstance(outcome, PendingHtmlFetch)
        # 改行 / 連続空白が単一スペースに正規化されていること
        assert "\n" not in outcome.title
        assert "    " not in outcome.title
        assert outcome.title.startswith("注意喚起: Cisco ASA")
        assert "(更新)" in outcome.title

    def test_does_not_strip_keishin_prefix(self) -> None:
        # ITmedia AI+ の `[ITmedia ...]` と異なり、"注意喚起:" は content
        outcome = self.fetcher._convert_entry(
            _entry(title="注意喚起: Foo Bar"), self.source, "ja"
        )
        assert isinstance(outcome, PendingHtmlFetch)
        assert outcome.title == "注意喚起: Foo Bar"

    def test_empty_title_returns_failed(self) -> None:
        outcome = self.fetcher._convert_entry(_entry(title=""), self.source, "ja")
        assert isinstance(outcome, Failed)
        assert outcome.reason.code == "title_missing"

    def test_whitespace_only_title_returns_failed(self) -> None:
        outcome = self.fetcher._convert_entry(
            _entry(title="   \n   "), self.source, "ja"
        )
        assert isinstance(outcome, Failed)
        assert outcome.reason.code == "title_missing"

    def test_invalid_link_returns_failed(self) -> None:
        outcome = self.fetcher._convert_entry(
            _entry(link="not-a-url"), self.source, "ja"
        )
        assert isinstance(outcome, Failed)
        assert outcome.reason.code == "extraction_empty"

    def test_published_parsed_yields_utc(self) -> None:
        # struct_time (UTC として扱われる) → tz=UTC PublishedAt
        outcome = self.fetcher._convert_entry(_entry(), self.source, "ja")
        assert isinstance(outcome, PendingHtmlFetch)
        assert outcome.published_at_hint is not None
        assert (
            outcome.published_at_hint.value.tzinfo.utcoffset(None).total_seconds() == 0
        )

    def test_missing_pubdate_yields_pending_with_none_hint(self) -> None:
        # Pattern H 緩い品質ゲート: pubDate 欠落でも Failed しない
        entry = _entry()
        del entry["published_parsed"]
        outcome = self.fetcher._convert_entry(entry, self.source, "ja")
        assert isinstance(outcome, PendingHtmlFetch)
        assert outcome.published_at_hint is None

    def test_metadata_author_is_none(self) -> None:
        outcome = self.fetcher._convert_entry(_entry(), self.source, "ja")
        assert isinstance(outcome, PendingHtmlFetch)
        # per-entry の author は未提供 (channel-level webmaster は使わない)
        assert outcome.metadata.author is None

    def test_metadata_tags_hardcoded_empty(self) -> None:
        outcome = self.fetcher._convert_entry(_entry(), self.source, "ja")
        assert isinstance(outcome, PendingHtmlFetch)
        assert outcome.metadata.tags == ()

    def test_metadata_image_url_hardcoded_none(self) -> None:
        outcome = self.fetcher._convert_entry(_entry(), self.source, "ja")
        assert isinstance(outcome, PendingHtmlFetch)
        assert outcome.metadata.image_url is None

    def test_extracts_guid_from_id(self) -> None:
        # rdf:about → entry.id マップ
        outcome = self.fetcher._convert_entry(_entry(), self.source, "ja")
        assert isinstance(outcome, PendingHtmlFetch)
        assert outcome.metadata.guid == (
            "https://www.jpcert.or.jp/at/2026/at260021.html"
        )

    def test_language_passthrough_ja(self) -> None:
        outcome = self.fetcher._convert_entry(_entry(), self.source, "ja")
        assert isinstance(outcome, PendingHtmlFetch)
        assert outcome.metadata.language == "ja"

    def test_site_name_hardcoded(self) -> None:
        outcome = self.fetcher._convert_entry(_entry(), self.source, "ja")
        assert isinstance(outcome, PendingHtmlFetch)
        assert outcome.metadata.site_name == "JPCERT/CC"


class TestFixtureParsing:
    def test_fixture_parses_as_rdf(self) -> None:
        # feedparser が RDF (RSS 1.0) を bozo にせず entries を 3 件取れること
        text = _FIXTURE.read_text(encoding="utf-8")
        feed = feedparser.parse(text)
        assert len(feed.entries) == 3

    def test_fixture_first_entry_yields_pending(self) -> None:
        text = _FIXTURE.read_text(encoding="utf-8")
        feed = feedparser.parse(text)
        fetcher = JPCERTFetcher()
        outcome = fetcher._convert_entry(feed.entries[0], _source(), "ja")
        assert isinstance(outcome, PendingHtmlFetch)
        # 多行 title が 1 行化されていること
        assert "\n" not in outcome.title
        assert outcome.title.startswith("注意喚起: Cisco ASA")
        assert outcome.metadata.language == "ja"
        assert outcome.metadata.site_name == "JPCERT/CC"

    def test_fixture_first_entry_guid_is_rdf_about_url(self) -> None:
        text = _FIXTURE.read_text(encoding="utf-8")
        feed = feedparser.parse(text)
        fetcher = JPCERTFetcher()
        outcome = fetcher._convert_entry(feed.entries[0], _source(), "ja")
        assert isinstance(outcome, PendingHtmlFetch)
        assert outcome.metadata.guid is not None
        assert outcome.metadata.guid.startswith("https://www.jpcert.or.jp/")

    def test_fixture_first_entry_has_published_hint(self) -> None:
        text = _FIXTURE.read_text(encoding="utf-8")
        feed = feedparser.parse(text)
        fetcher = JPCERTFetcher()
        outcome = fetcher._convert_entry(feed.entries[0], _source(), "ja")
        assert isinstance(outcome, PendingHtmlFetch)
        # ISO 8601 +09:00 → feedparser が UTC 換算して published_parsed を出す
        assert outcome.published_at_hint is not None
        assert (
            outcome.published_at_hint.value.tzinfo.utcoffset(None).total_seconds() == 0
        )

    def test_fixture_third_entry_empty_title_yields_failed(self) -> None:
        text = _FIXTURE.read_text(encoding="utf-8")
        feed = feedparser.parse(text)
        fetcher = JPCERTFetcher()
        outcome = fetcher._convert_entry(feed.entries[2], _source(), "ja")
        assert isinstance(outcome, Failed)
        assert outcome.reason.code == "title_missing"
