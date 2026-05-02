"""``EngadgetFetcher`` の単体テスト (Phase 1c-C)。

per-source 設計:
- body は読まない (RSS の `<content:encoded>` は ~50 chars caption 偽陽性)
- author / tags / image_url / guid は RSS 提供あり (TC と同形)
- PROVIDES は TC と同じ {language, guid, site_name}
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
from app.collection.ingestion.fetchers.engadget import EngadgetFetcher

_FIXTURE = Path(__file__).parent.parent.parent.parent / "fixtures" / "engadget_rss.xml"


_SOURCE_ID = 1


def _entry(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "title": "Apple Vision Pro 2 review",
        "link": "https://www.engadget.com/apple-vision-pro-2-review-2026.html",
        "id": "engadget-2026-04-30-vision-pro-2",
        "summary": "Apple's second-gen mixed-reality headset is here.",
        "published_parsed": time.struct_time((2026, 4, 30, 14, 0, 0, 0, 0, 0)),
        "author": "Sam Rutherford",
        "tags": [{"term": "Reviews"}, {"term": "Wearables"}],
        "media_content": [
            {"url": "https://s.yimg.com/os/creatr-uploaded-images/vision-pro-2.jpg"}
        ],
    }
    base.update(overrides)
    return base


class TestProvides:
    def test_provides_minimum_set(self) -> None:
        assert EngadgetFetcher.PROVIDES == frozenset({"language", "guid", "site_name"})


class TestConvertEntry:
    def setup_method(self) -> None:
        self.fetcher = EngadgetFetcher()
        self.source_id = _SOURCE_ID

    def test_valid_entry_yields_pending(self) -> None:
        outcome = self.fetcher._convert_entry(_entry(), self.source_id, "en-US")
        assert isinstance(outcome, PendingHtmlFetch)
        assert outcome.title == "Apple Vision Pro 2 review"
        assert outcome.published_at_hint is not None

    def test_does_not_construct_body(self) -> None:
        outcome = self.fetcher._convert_entry(_entry(), self.source_id, "en-US")
        assert isinstance(outcome, PendingHtmlFetch)
        assert not hasattr(outcome, "body")

    def test_empty_title_returns_failed(self) -> None:
        outcome = self.fetcher._convert_entry(_entry(title=""), self.source_id, "en-US")
        assert isinstance(outcome, Failed)
        assert outcome.reason.code == "title_missing"

    def test_invalid_link_returns_failed(self) -> None:
        outcome = self.fetcher._convert_entry(
            _entry(link="not-a-url"), self.source_id, "en-US"
        )
        assert isinstance(outcome, Failed)
        assert outcome.reason.code == "extraction_empty"

    def test_extracts_author(self) -> None:
        outcome = self.fetcher._convert_entry(_entry(), self.source_id, "en-US")
        assert isinstance(outcome, PendingHtmlFetch)
        assert outcome.metadata.author == "Sam Rutherford"

    def test_extracts_tags(self) -> None:
        outcome = self.fetcher._convert_entry(_entry(), self.source_id, "en-US")
        assert isinstance(outcome, PendingHtmlFetch)
        assert outcome.metadata.tags == ("Reviews", "Wearables")

    def test_extracts_image_url(self) -> None:
        outcome = self.fetcher._convert_entry(_entry(), self.source_id, "en-US")
        assert isinstance(outcome, PendingHtmlFetch)
        assert outcome.metadata.image_url is not None

    def test_extracts_guid(self) -> None:
        outcome = self.fetcher._convert_entry(_entry(), self.source_id, "en-US")
        assert isinstance(outcome, PendingHtmlFetch)
        assert outcome.metadata.guid == "engadget-2026-04-30-vision-pro-2"

    def test_site_name_hardcoded(self) -> None:
        outcome = self.fetcher._convert_entry(_entry(), self.source_id, "en-US")
        assert isinstance(outcome, PendingHtmlFetch)
        assert outcome.metadata.site_name == "Engadget"


class TestFixtureParsing:
    def test_fixture_first_entry_yields_pending(self) -> None:
        text = _FIXTURE.read_text(encoding="utf-8")
        feed = feedparser.parse(text)
        fetcher = EngadgetFetcher()
        outcome = fetcher._convert_entry(feed.entries[0], _SOURCE_ID, "en-US")
        assert isinstance(outcome, PendingHtmlFetch)
        assert outcome.title.startswith("Apple Vision Pro 2")
        assert outcome.metadata.author == "Sam Rutherford"
        assert "Reviews" in outcome.metadata.tags
        assert outcome.metadata.image_url is not None
        assert outcome.metadata.guid == "engadget-2026-04-30-vision-pro-2"
        assert outcome.metadata.language == "en-US"

    def test_fixture_third_entry_empty_title_yields_failed(self) -> None:
        text = _FIXTURE.read_text(encoding="utf-8")
        feed = feedparser.parse(text)
        fetcher = EngadgetFetcher()
        outcome = fetcher._convert_entry(feed.entries[2], _SOURCE_ID, "en-US")
        assert isinstance(outcome, Failed)
        assert outcome.reason.code == "title_missing"
