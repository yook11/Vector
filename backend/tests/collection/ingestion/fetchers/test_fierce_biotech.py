"""``FierceBiotechFetcher`` の単体テスト (Phase 1c-D)。

per-source 設計:
- pubDate 非標準形式 ("Apr 30, 2026 6:11pm") を strptime fallback + ET → UTC 換算
- title / author が ``<a>`` 要素で wrap されているため両方に ``_strip_html`` 適用
- tags = () / image_url = None 直書き (RSS 未提供)
- ``<guid isPermaLink="true">`` (UUID URL) 提供あり → PROVIDES に guid 含む
- language は feed-level "en" (NOT "en-US")
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import feedparser

from app.collection.ingestion.domain.fetched_article import (
    Failed,
    PendingHtmlFetch,
)
from app.collection.ingestion.fetchers.fierce_biotech import (
    FierceBiotechFetcher,
    _parse_published_at,
)

_FIXTURE = (
    Path(__file__).parent.parent.parent.parent / "fixtures" / "fierce_biotech_rss.xml"
)
_ET = ZoneInfo("America/New_York")


_SOURCE_ID = 1


def _entry(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "title": '<a href="/biotech/amgen">Amgen abandons late-stage asset</a>',
        "link": "https://www.fiercebiotech.com/biotech/amgen",
        "id": "https://www.fiercebiotech.com/534b86f8-9d4e-4a1f-8c20-1234567890ab",
        "summary": "Amgen has discontinued development of its late-stage candidate.",
        "published": "Apr 30, 2026 6:11pm",
        "author": '<a href="/person/darren-incorvaia">Darren Incorvaia</a>',
    }
    base.update(overrides)
    return base


class TestParsePublishedAt:
    """``_parse_published_at`` の単体テスト (FB 固有の strptime fallback)。"""

    def test_dst_period_applies_et_offset_4h(self) -> None:
        # 4 月 30 日 18:11 ET (DST 期間) → UTC 22:11 (+4h)
        out = _parse_published_at({"published": "Apr 30, 2026 6:11pm"})
        assert out is not None
        expected = datetime(2026, 4, 30, 18, 11, tzinfo=_ET).astimezone(UTC)
        assert out.value == expected
        assert out.value.hour == 22  # ET DST → UTC +4

    def test_dst_period_pm_lowercase(self) -> None:
        out = _parse_published_at({"published": "Apr 30, 2026 1:18pm"})
        assert out is not None
        # 13:18 ET DST → 17:18 UTC
        assert out.value.hour == 17
        assert out.value.minute == 18

    def test_non_dst_period_applies_et_offset_5h(self) -> None:
        # 1 月 15 日 9:00 ET (非 DST) → UTC 14:00 (+5h)
        out = _parse_published_at({"published": "Jan 15, 2026 9:00am"})
        assert out is not None
        assert out.value.hour == 14
        assert out.value.minute == 0
        assert out.value.tzinfo == UTC

    def test_missing_published_returns_none(self) -> None:
        assert _parse_published_at({}) is None

    def test_garbage_string_returns_none(self) -> None:
        assert _parse_published_at({"published": "garbage"}) is None

    def test_empty_string_returns_none(self) -> None:
        assert _parse_published_at({"published": ""}) is None

    def test_feedparser_path_returns_utc(self) -> None:
        # feedparser が解釈成功した場合 (将来 RFC822 化された場合の自動吸収)
        out = _parse_published_at(
            {"published_parsed": time.struct_time((2026, 4, 30, 22, 11, 0, 0, 0, 0))}
        )
        assert out is not None
        assert out.value.tzinfo == UTC
        assert out.value.hour == 22

    def test_updated_fallback(self) -> None:
        # ``published`` 不在で ``updated`` がある場合
        out = _parse_published_at({"updated": "Apr 30, 2026 6:11pm"})
        assert out is not None
        assert out.value.hour == 22


class TestProvides:
    def test_provides_minimum_set(self) -> None:
        assert FierceBiotechFetcher.PROVIDES == frozenset(
            {"language", "guid", "site_name"}
        )


class TestConvertEntry:
    def setup_method(self) -> None:
        self.fetcher = FierceBiotechFetcher()
        self.source_id = _SOURCE_ID

    def test_valid_entry_yields_pending(self) -> None:
        outcome = self.fetcher._convert_entry(_entry(), self.source_id, "en")
        assert isinstance(outcome, PendingHtmlFetch)
        assert outcome.published_at_hint is not None

    def test_does_not_construct_body(self) -> None:
        outcome = self.fetcher._convert_entry(_entry(), self.source_id, "en")
        assert isinstance(outcome, PendingHtmlFetch)
        assert not hasattr(outcome, "body")

    def test_title_strips_html_wrapping(self) -> None:
        outcome = self.fetcher._convert_entry(_entry(), self.source_id, "en")
        assert isinstance(outcome, PendingHtmlFetch)
        assert "<a href=" not in outcome.title
        assert outcome.title == "Amgen abandons late-stage asset"

    def test_empty_title_returns_failed(self) -> None:
        outcome = self.fetcher._convert_entry(_entry(title=""), self.source_id, "en")
        assert isinstance(outcome, Failed)
        assert outcome.reason.code == "title_missing"

    def test_html_only_title_returns_failed(self) -> None:
        # HTML を strip すると空になるケース
        outcome = self.fetcher._convert_entry(
            _entry(title="<a href='/x'></a>"), self.source_id, "en"
        )
        assert isinstance(outcome, Failed)
        assert outcome.reason.code == "title_missing"

    def test_invalid_link_returns_failed(self) -> None:
        outcome = self.fetcher._convert_entry(
            _entry(link="not-a-url"), self.source_id, "en"
        )
        assert isinstance(outcome, Failed)
        assert outcome.reason.code == "extraction_empty"

    def test_missing_pubdate_yields_pending_with_none_hint(self) -> None:
        # Pattern H 緩い品質ゲート: pubDate 欠落でも Failed しない
        entry = _entry()
        del entry["published"]
        outcome = self.fetcher._convert_entry(entry, self.source_id, "en")
        assert isinstance(outcome, PendingHtmlFetch)
        assert outcome.published_at_hint is None

    def test_author_strips_html_wrapping(self) -> None:
        outcome = self.fetcher._convert_entry(_entry(), self.source_id, "en")
        assert isinstance(outcome, PendingHtmlFetch)
        assert outcome.metadata.author == "Darren Incorvaia"

    def test_author_missing_returns_none(self) -> None:
        entry = _entry()
        del entry["author"]
        outcome = self.fetcher._convert_entry(entry, self.source_id, "en")
        assert isinstance(outcome, PendingHtmlFetch)
        assert outcome.metadata.author is None

    def test_tags_hardcoded_empty(self) -> None:
        outcome = self.fetcher._convert_entry(_entry(), self.source_id, "en")
        assert isinstance(outcome, PendingHtmlFetch)
        assert outcome.metadata.tags == ()

    def test_image_url_hardcoded_none(self) -> None:
        outcome = self.fetcher._convert_entry(_entry(), self.source_id, "en")
        assert isinstance(outcome, PendingHtmlFetch)
        assert outcome.metadata.image_url is None

    def test_extracts_guid_from_id(self) -> None:
        outcome = self.fetcher._convert_entry(_entry(), self.source_id, "en")
        assert isinstance(outcome, PendingHtmlFetch)
        assert outcome.metadata.guid is not None
        assert outcome.metadata.guid.startswith("https://www.fiercebiotech.com/")

    def test_language_passthrough_en(self) -> None:
        outcome = self.fetcher._convert_entry(_entry(), self.source_id, "en")
        assert isinstance(outcome, PendingHtmlFetch)
        assert outcome.metadata.language == "en"

    def test_site_name_hardcoded(self) -> None:
        outcome = self.fetcher._convert_entry(_entry(), self.source_id, "en")
        assert isinstance(outcome, PendingHtmlFetch)
        assert outcome.metadata.site_name == "FierceBiotech"


class TestFixtureParsing:
    def test_fixture_first_entry_yields_pending(self) -> None:
        text = _FIXTURE.read_text(encoding="utf-8")
        feed = feedparser.parse(text)
        fetcher = FierceBiotechFetcher()
        outcome = fetcher._convert_entry(feed.entries[0], _SOURCE_ID, "en")
        assert isinstance(outcome, PendingHtmlFetch)
        assert outcome.title == "Amgen abandons late-stage asset after trial flop"
        assert outcome.metadata.author == "Darren Incorvaia"
        assert outcome.metadata.tags == ()
        assert outcome.metadata.image_url is None
        assert outcome.metadata.language == "en"
        assert outcome.metadata.guid is not None
        assert outcome.metadata.guid.startswith("https://www.fiercebiotech.com/")

    def test_fixture_first_entry_has_published_hint(self) -> None:
        text = _FIXTURE.read_text(encoding="utf-8")
        feed = feedparser.parse(text)
        fetcher = FierceBiotechFetcher()
        outcome = fetcher._convert_entry(feed.entries[0], _SOURCE_ID, "en")
        assert isinstance(outcome, PendingHtmlFetch)
        # "Apr 30, 2026 6:11pm" ET DST → UTC 22:11
        assert outcome.published_at_hint is not None
        assert outcome.published_at_hint.value.hour == 22
        assert outcome.published_at_hint.value.minute == 11
        assert outcome.published_at_hint.value.tzinfo == UTC

    def test_fixture_third_entry_empty_title_yields_failed(self) -> None:
        text = _FIXTURE.read_text(encoding="utf-8")
        feed = feedparser.parse(text)
        fetcher = FierceBiotechFetcher()
        outcome = fetcher._convert_entry(feed.entries[2], _SOURCE_ID, "en")
        assert isinstance(outcome, Failed)
        assert outcome.reason.code == "title_missing"
