"""``CleanTechnicaFetcher`` の単体テスト (Phase 1d)。

per-source 設計:
- WordPress 系 RSS 2.0、本文は HTML 必須 (Pattern H 設計)
- ``<dc:creator>`` を author に採用
- ``<category>`` 多数を tags に採用
- ``<media:>`` namespace 未提供 → image_url=None 直書き
- ``<guid isPermaLink="false">`` (``?p=<id>`` 形式) を採用
- language は feed-level "en-US"
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
from app.collection.ingestion.fetchers.cleantechnica import CleanTechnicaFetcher

_FIXTURE = (
    Path(__file__).parent.parent.parent.parent / "fixtures" / "cleantechnica_rss.xml"
)


_SOURCE_ID = 1


def _entry(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "title": "Scope 3 Emissions: Challenging? Yes.",
        "link": "https://cleantechnica.com/2026/05/01/scope-3-emissions/",
        "id": "https://cleantechnica.com/?p=372217",
        "author": "Carolyn Fortuna",
        "published_parsed": time.struct_time((2026, 5, 1, 21, 11, 20, 0, 0, 0)),
        "tags": [
            {"term": "Green Economy", "scheme": None, "label": None},
            {"term": "Manufacturing", "scheme": None, "label": None},
        ],
    }
    base.update(overrides)
    return base


class TestProvides:
    def test_provides_minimum_set(self) -> None:
        assert CleanTechnicaFetcher.PROVIDES == frozenset(
            {"language", "guid", "site_name"}
        )


class TestConvertEntry:
    def setup_method(self) -> None:
        self.fetcher = CleanTechnicaFetcher()
        self.source_id = _SOURCE_ID

    def test_valid_entry_yields_pending(self) -> None:
        outcome = self.fetcher._convert_entry(_entry(), self.source_id, "en-US")
        assert isinstance(outcome, PendingHtmlFetch)
        assert outcome.title.startswith("Scope 3 Emissions")

    def test_does_not_construct_body(self) -> None:
        outcome = self.fetcher._convert_entry(_entry(), self.source_id, "en-US")
        assert isinstance(outcome, PendingHtmlFetch)
        assert not hasattr(outcome, "body")

    def test_empty_title_returns_failed(self) -> None:
        outcome = self.fetcher._convert_entry(_entry(title=""), self.source_id, "en-US")
        assert isinstance(outcome, Failed)
        assert outcome.reason.code == "title_missing"

    def test_whitespace_only_title_returns_failed(self) -> None:
        outcome = self.fetcher._convert_entry(
            _entry(title="   \n   "), self.source_id, "en-US"
        )
        assert isinstance(outcome, Failed)
        assert outcome.reason.code == "title_missing"

    def test_invalid_link_returns_failed(self) -> None:
        outcome = self.fetcher._convert_entry(
            _entry(link="not-a-url"), self.source_id, "en-US"
        )
        assert isinstance(outcome, Failed)
        assert outcome.reason.code == "extraction_empty"

    def test_published_parsed_yields_utc(self) -> None:
        outcome = self.fetcher._convert_entry(_entry(), self.source_id, "en-US")
        assert isinstance(outcome, PendingHtmlFetch)
        assert outcome.published_at_hint is not None
        assert (
            outcome.published_at_hint.value.tzinfo.utcoffset(None).total_seconds() == 0
        )

    def test_missing_pubdate_yields_pending_with_none_hint(self) -> None:
        # Pattern H 緩い品質ゲート: pubDate 欠落でも Failed しない
        entry = _entry()
        del entry["published_parsed"]
        outcome = self.fetcher._convert_entry(entry, self.source_id, "en-US")
        assert isinstance(outcome, PendingHtmlFetch)
        assert outcome.published_at_hint is None

    def test_metadata_author_from_dc_creator(self) -> None:
        outcome = self.fetcher._convert_entry(_entry(), self.source_id, "en-US")
        assert isinstance(outcome, PendingHtmlFetch)
        assert outcome.metadata.author == "Carolyn Fortuna"

    def test_metadata_tags_from_category(self) -> None:
        outcome = self.fetcher._convert_entry(_entry(), self.source_id, "en-US")
        assert isinstance(outcome, PendingHtmlFetch)
        assert outcome.metadata.tags == ("Green Economy", "Manufacturing")

    def test_metadata_image_url_hardcoded_none(self) -> None:
        outcome = self.fetcher._convert_entry(_entry(), self.source_id, "en-US")
        assert isinstance(outcome, PendingHtmlFetch)
        assert outcome.metadata.image_url is None

    def test_extracts_guid_from_id(self) -> None:
        outcome = self.fetcher._convert_entry(_entry(), self.source_id, "en-US")
        assert isinstance(outcome, PendingHtmlFetch)
        assert outcome.metadata.guid == "https://cleantechnica.com/?p=372217"

    def test_language_passthrough_en_us(self) -> None:
        outcome = self.fetcher._convert_entry(_entry(), self.source_id, "en-US")
        assert isinstance(outcome, PendingHtmlFetch)
        assert outcome.metadata.language == "en-US"

    def test_site_name_hardcoded(self) -> None:
        outcome = self.fetcher._convert_entry(_entry(), self.source_id, "en-US")
        assert isinstance(outcome, PendingHtmlFetch)
        assert outcome.metadata.site_name == "CleanTechnica"


class TestFixtureParsing:
    def test_fixture_first_entry_yields_pending(self) -> None:
        text = _FIXTURE.read_text(encoding="utf-8")
        feed = feedparser.parse(text)
        fetcher = CleanTechnicaFetcher()
        outcome = fetcher._convert_entry(feed.entries[0], _SOURCE_ID, "en-US")
        assert isinstance(outcome, PendingHtmlFetch)
        assert outcome.title.startswith("Scope 3 Emissions")
        assert outcome.metadata.language == "en-US"
        assert outcome.metadata.site_name == "CleanTechnica"
        assert outcome.metadata.author == "Carolyn Fortuna"
        assert "Green Economy" in outcome.metadata.tags

    def test_fixture_first_entry_guid_is_wp_post_id(self) -> None:
        text = _FIXTURE.read_text(encoding="utf-8")
        feed = feedparser.parse(text)
        fetcher = CleanTechnicaFetcher()
        outcome = fetcher._convert_entry(feed.entries[0], _SOURCE_ID, "en-US")
        assert isinstance(outcome, PendingHtmlFetch)
        assert outcome.metadata.guid is not None
        assert "cleantechnica.com" in outcome.metadata.guid

    def test_fixture_third_entry_empty_title_yields_failed(self) -> None:
        text = _FIXTURE.read_text(encoding="utf-8")
        feed = feedparser.parse(text)
        fetcher = CleanTechnicaFetcher()
        outcome = fetcher._convert_entry(feed.entries[2], _SOURCE_ID, "en-US")
        assert isinstance(outcome, Failed)
        assert outcome.reason.code == "title_missing"
