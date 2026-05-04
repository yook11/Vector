"""``METIFetcher`` の単体テスト (Phase 3 PR 3-h-2)。

per-source 設計:
- Atom 1.0 feed 形式を feedparser 標準経路で解釈 (UTF-8)
- ``<entry><id>`` 不在時は ``<link href>`` を ``entry.id`` に流用される
  feedparser 既定動作を guid 採用 (RDF と異なる Atom 仕様)
- ``<updated>`` (Atom 必須) → ``updated_parsed`` 経由で ``published_at_hint``
- author / tags / image_url は per-entry で未提供のため None / () 直書き
- PROVIDES = {language, guid, site_name} (MEXT/MIC と同形)
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
from app.collection.ingestion.fetchers.meti import METIFetcher

_FIXTURE = Path(__file__).parent.parent.parent.parent / "fixtures" / "meti_atom.xml"

_SOURCE_ID = 1


def _entry(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "title": "高性能AIへの対応に関する意見交換を実施しました",
        "link": "https://www.meti.go.jp/press/2026/05/20260501001/20260501001.html",
        "id": "https://www.meti.go.jp/press/2026/05/20260501001/20260501001.html",
        "updated_parsed": time.struct_time((2026, 5, 1, 10, 58, 0, 0, 0, 0)),
    }
    base.update(overrides)
    return base


class TestProvides:
    def test_provides_minimum_set(self) -> None:
        assert METIFetcher.PROVIDES == frozenset({"language", "guid", "site_name"})


class TestConvertEntry:
    def setup_method(self) -> None:
        self.fetcher = METIFetcher()
        self.source_id = _SOURCE_ID

    def test_valid_entry_yields_pending(self) -> None:
        outcome = self.fetcher._convert_entry(_entry(), self.source_id, "ja")
        assert isinstance(outcome, PendingHtmlFetch)
        assert outcome.title.startswith("高性能AI")

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

    def test_updated_parsed_yields_utc(self) -> None:
        outcome = self.fetcher._convert_entry(_entry(), self.source_id, "ja")
        assert isinstance(outcome, PendingHtmlFetch)
        assert outcome.published_at_hint is not None
        assert (
            outcome.published_at_hint.value.tzinfo.utcoffset(None).total_seconds() == 0
        )

    def test_falls_back_to_published_parsed(self) -> None:
        # Atom 仕様外だが一部 feed が ``<published>`` を併記するケース
        entry = _entry()
        del entry["updated_parsed"]
        entry["published_parsed"] = time.struct_time((2026, 4, 30, 12, 0, 0, 0, 0, 0))
        outcome = self.fetcher._convert_entry(entry, self.source_id, "ja")
        assert isinstance(outcome, PendingHtmlFetch)
        assert outcome.published_at_hint is not None
        assert outcome.published_at_hint.value.year == 2026
        assert outcome.published_at_hint.value.month == 4

    def test_missing_pubdate_yields_pending_with_none_hint(self) -> None:
        # Pattern H 緩い品質ゲート: pubDate 欠落でも Failed しない
        entry = _entry()
        del entry["updated_parsed"]
        outcome = self.fetcher._convert_entry(entry, self.source_id, "ja")
        assert isinstance(outcome, PendingHtmlFetch)
        assert outcome.published_at_hint is None

    def test_metadata_author_is_none(self) -> None:
        # METI feed の <author><name /></author> は空、metadata.author は None
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
            "https://www.meti.go.jp/press/2026/05/20260501001/20260501001.html"
        )

    def test_guid_falls_back_to_link_when_id_missing(self) -> None:
        # Atom <id> 欠落時は <link href> を guid に流用
        entry = _entry()
        del entry["id"]
        outcome = self.fetcher._convert_entry(entry, self.source_id, "ja")
        assert isinstance(outcome, PendingHtmlFetch)
        assert outcome.metadata.guid == entry["link"]

    def test_language_passthrough_ja(self) -> None:
        outcome = self.fetcher._convert_entry(_entry(), self.source_id, "ja")
        assert isinstance(outcome, PendingHtmlFetch)
        assert outcome.metadata.language == "ja"

    def test_site_name_hardcoded(self) -> None:
        outcome = self.fetcher._convert_entry(_entry(), self.source_id, "ja")
        assert isinstance(outcome, PendingHtmlFetch)
        assert outcome.metadata.site_name == "METI"


class TestFixtureParsing:
    def test_fixture_parses_as_atom(self) -> None:
        # bytes 経由で feedparser に渡し、Atom として entries を 3 件取れること
        data = _FIXTURE.read_bytes()
        feed = feedparser.parse(data)
        assert len(feed.entries) == 3

    def test_fixture_first_entry_yields_pending(self) -> None:
        data = _FIXTURE.read_bytes()
        feed = feedparser.parse(data)
        fetcher = METIFetcher()
        outcome = fetcher._convert_entry(feed.entries[0], _SOURCE_ID, "ja")
        assert isinstance(outcome, PendingHtmlFetch)
        assert outcome.title.startswith("高性能AI")
        assert outcome.metadata.language == "ja"
        assert outcome.metadata.site_name == "METI"

    def test_fixture_first_entry_guid_is_link_url(self) -> None:
        data = _FIXTURE.read_bytes()
        feed = feedparser.parse(data)
        fetcher = METIFetcher()
        outcome = fetcher._convert_entry(feed.entries[0], _SOURCE_ID, "ja")
        assert isinstance(outcome, PendingHtmlFetch)
        assert outcome.metadata.guid is not None
        assert outcome.metadata.guid.startswith("https://www.meti.go.jp/press/")

    def test_fixture_first_entry_has_published_hint(self) -> None:
        data = _FIXTURE.read_bytes()
        feed = feedparser.parse(data)
        fetcher = METIFetcher()
        outcome = fetcher._convert_entry(feed.entries[0], _SOURCE_ID, "ja")
        assert isinstance(outcome, PendingHtmlFetch)
        assert outcome.published_at_hint is not None
        # 2026-05-01T10:58:00Z
        assert outcome.published_at_hint.value.year == 2026
        assert outcome.published_at_hint.value.day == 1

    def test_fixture_third_entry_empty_title_yields_failed(self) -> None:
        data = _FIXTURE.read_bytes()
        feed = feedparser.parse(data)
        fetcher = METIFetcher()
        outcome = fetcher._convert_entry(feed.entries[2], _SOURCE_ID, "ja")
        assert isinstance(outcome, Failed)
        assert outcome.reason.code == "title_missing"
