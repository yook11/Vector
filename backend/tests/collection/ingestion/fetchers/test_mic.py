"""``MICFetcher`` の単体テスト (Phase 3 PR 3-h-1)。

per-source 設計:
- RDF (RSS 1.0) feed 形式、**Shift_JIS** エンコード
- ``feedparser.parse(response.content)`` (bytes) 必須 → encoding sniff
- ``<item rdf:about="URL">`` を ``entry.id`` にマップ → guid に採用
- author / tags / image_url は per-entry で未提供のため None / () 直書き
- ``<description>`` が ``<title>`` と同一 (本文ゼロ) でも summary は拾わない
  (Pattern H 統一設計)
- PROVIDES = {language, guid, site_name}
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
from app.collection.ingestion.fetchers.mic import MICFetcher

_FIXTURE = Path(__file__).parent.parent.parent.parent / "fixtures" / "mic_rdf.xml"

_SOURCE_ID = 1


def _entry(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "title": "サイバーセキュリティタスクフォース(第50回)配布資料",
        "link": "https://www.soumu.go.jp/menu_news/s-news/01kiban02_02000301.html",
        "id": "https://www.soumu.go.jp/menu_news/s-news/01kiban02_02000301.html",
        "published_parsed": time.struct_time((2026, 5, 1, 6, 0, 0, 0, 0, 0)),
    }
    base.update(overrides)
    return base


class TestProvides:
    def test_provides_minimum_set(self) -> None:
        assert MICFetcher.PROVIDES == frozenset({"language", "guid", "site_name"})


class TestConvertEntry:
    def setup_method(self) -> None:
        self.fetcher = MICFetcher()
        self.source_id = _SOURCE_ID

    def test_valid_entry_yields_pending(self) -> None:
        outcome = self.fetcher._convert_entry(_entry(), self.source_id, "ja")
        assert isinstance(outcome, PendingHtmlFetch)
        assert outcome.title.startswith("サイバーセキュリティ")

    def test_does_not_construct_body(self) -> None:
        outcome = self.fetcher._convert_entry(_entry(), self.source_id, "ja")
        assert isinstance(outcome, PendingHtmlFetch)
        assert not hasattr(outcome, "body")

    def test_empty_title_returns_failed(self) -> None:
        outcome = self.fetcher._convert_entry(_entry(title=""), self.source_id, "ja")
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
            "https://www.soumu.go.jp/menu_news/s-news/01kiban02_02000301.html"
        )

    def test_site_name_hardcoded(self) -> None:
        outcome = self.fetcher._convert_entry(_entry(), self.source_id, "ja")
        assert isinstance(outcome, PendingHtmlFetch)
        assert outcome.metadata.site_name == "MIC"


class TestShiftJisFixtureParsing:
    """Shift_JIS bytes を bytes 経由で渡したとき、文字化けせず解釈されること。"""

    def test_fixture_is_actually_shift_jis(self) -> None:
        # fixture ファイルが UTF-8 として decode 失敗、Shift_JIS なら成功すること
        data = _FIXTURE.read_bytes()
        try:
            data.decode("utf-8")
            raise AssertionError("fixture must NOT be decodable as UTF-8")
        except UnicodeDecodeError:
            pass
        text = data.decode("shift_jis")
        assert "総務省" in text

    def test_fixture_parses_as_rdf_via_bytes(self) -> None:
        # bytes 経由なら feedparser は XML 宣言から Shift_JIS を sniff する
        data = _FIXTURE.read_bytes()
        feed = feedparser.parse(data)
        assert len(feed.entries) == 3

    def test_fixture_first_entry_title_decoded_correctly(self) -> None:
        # 文字化けせず日本語が正しく解釈されていること
        data = _FIXTURE.read_bytes()
        feed = feedparser.parse(data)
        fetcher = MICFetcher()
        outcome = fetcher._convert_entry(feed.entries[0], _SOURCE_ID, "ja")
        assert isinstance(outcome, PendingHtmlFetch)
        assert outcome.title.startswith("サイバーセキュリティ")
        # mojibake (例: 縺) が含まれていないこと
        assert "縺" not in outcome.title
        assert "?" not in outcome.title

    def test_fixture_text_path_breaks_decoding(self) -> None:
        # 反証: response.text 相当 (UTF-8 として decode → feedparser) では失敗
        # する。これが bytes 経由を採用する理由
        data = _FIXTURE.read_bytes()
        # UTF-8 fallback で強制デコード (errors=replace で文字化け)
        text = data.decode("utf-8", errors="replace")
        feed = feedparser.parse(text)
        # entries 自体は取れるかもしれないが title が文字化け
        # bytes 経由 (テスト 2) との差を契約として固定
        if feed.entries:
            title = feed.entries[0].get("title", "")
            assert "サイバーセキュリティ" not in title or "�" in title

    def test_fixture_third_entry_empty_title_yields_failed(self) -> None:
        data = _FIXTURE.read_bytes()
        feed = feedparser.parse(data)
        fetcher = MICFetcher()
        outcome = fetcher._convert_entry(feed.entries[2], _SOURCE_ID, "ja")
        assert isinstance(outcome, Failed)
        assert outcome.reason.code == "title_missing"
