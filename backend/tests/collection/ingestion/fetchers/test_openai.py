"""``OpenAIFetcher`` の単体テスト (Phase 3 PR 3-d-2)。

per-source 設計:
- RSS 2.0、``<description>`` 短い概要のみ → Pattern H
- ``<author>`` 未提供 → ``metadata.author = "OpenAI"`` hardcode
- ``<category>`` を ``metadata.tags`` に詰める
- ``<language>`` 未提供 → default "en" hardcode
- PROVIDES = {language, guid, site_name, author}
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
from app.collection.ingestion.fetchers.openai import OpenAIFetcher

_FIXTURE = Path(__file__).parent.parent.parent.parent / "fixtures" / "openai_rss.xml"

_SOURCE_ID = 1


def _entry(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "title": "Introducing Test Article",
        "link": "https://openai.com/index/test-article",
        "id": "https://openai.com/index/test-article",
        "published_parsed": time.struct_time((2026, 4, 30, 0, 0, 0, 0, 0, 0)),
        "tags": [{"term": "Product"}],
    }
    base.update(overrides)
    return base


class TestProvides:
    def test_provides_includes_author(self) -> None:
        assert OpenAIFetcher.PROVIDES == frozenset(
            {"language", "guid", "site_name", "author"}
        )

    def test_endpoint_is_canonical_news_feed(self) -> None:
        assert OpenAIFetcher.ENDPOINT_URL == "https://openai.com/news/rss.xml"


class TestConvertEntry:
    def setup_method(self) -> None:
        self.fetcher = OpenAIFetcher()

    def test_valid_entry_yields_pending(self) -> None:
        outcome = self.fetcher._convert_entry(_entry(), _SOURCE_ID, "en")
        assert isinstance(outcome, PendingHtmlFetch)
        assert outcome.title == "Introducing Test Article"

    def test_does_not_construct_body(self) -> None:
        outcome = self.fetcher._convert_entry(_entry(), _SOURCE_ID, "en")
        assert isinstance(outcome, PendingHtmlFetch)
        assert not hasattr(outcome, "body")

    def test_empty_title_returns_failed(self) -> None:
        outcome = self.fetcher._convert_entry(_entry(title=""), _SOURCE_ID, "en")
        assert isinstance(outcome, Failed)
        assert outcome.reason.code == "title_missing"

    def test_invalid_link_returns_failed(self) -> None:
        outcome = self.fetcher._convert_entry(
            _entry(link="not-a-url"), _SOURCE_ID, "en"
        )
        assert isinstance(outcome, Failed)
        assert outcome.reason.code == "extraction_empty"

    def test_metadata_author_hardcoded(self) -> None:
        outcome = self.fetcher._convert_entry(_entry(), _SOURCE_ID, "en")
        assert isinstance(outcome, PendingHtmlFetch)
        assert outcome.metadata.author == "OpenAI"

    def test_metadata_tags_extracted(self) -> None:
        outcome = self.fetcher._convert_entry(
            _entry(tags=[{"term": "Research"}, {"term": "Safety"}]), _SOURCE_ID, "en"
        )
        assert isinstance(outcome, PendingHtmlFetch)
        assert outcome.metadata.tags == ("Research", "Safety")

    def test_metadata_tags_empty_when_missing(self) -> None:
        entry = _entry()
        del entry["tags"]
        outcome = self.fetcher._convert_entry(entry, _SOURCE_ID, "en")
        assert isinstance(outcome, PendingHtmlFetch)
        assert outcome.metadata.tags == ()

    def test_metadata_image_url_hardcoded_none(self) -> None:
        outcome = self.fetcher._convert_entry(_entry(), _SOURCE_ID, "en")
        assert isinstance(outcome, PendingHtmlFetch)
        assert outcome.metadata.image_url is None

    def test_extracts_guid_from_id(self) -> None:
        outcome = self.fetcher._convert_entry(_entry(), _SOURCE_ID, "en")
        assert isinstance(outcome, PendingHtmlFetch)
        assert outcome.metadata.guid == "https://openai.com/index/test-article"

    def test_site_name_hardcoded(self) -> None:
        outcome = self.fetcher._convert_entry(_entry(), _SOURCE_ID, "en")
        assert isinstance(outcome, PendingHtmlFetch)
        assert outcome.metadata.site_name == "OpenAI"


class TestFixtureParsing:
    def test_fixture_parses_three_entries(self) -> None:
        feed = feedparser.parse(_FIXTURE.read_bytes())
        assert len(feed.entries) == 3

    def test_fixture_first_entry_is_pattern_h_pending(self) -> None:
        feed = feedparser.parse(_FIXTURE.read_bytes())
        fetcher = OpenAIFetcher()
        outcome = fetcher._convert_entry(feed.entries[0], _SOURCE_ID, "en")
        assert isinstance(outcome, PendingHtmlFetch)
        assert outcome.title.startswith("Introducing Advanced Account Security")
        assert outcome.metadata.author == "OpenAI"
        assert outcome.metadata.site_name == "OpenAI"

    def test_fixture_first_entry_has_tags(self) -> None:
        feed = feedparser.parse(_FIXTURE.read_bytes())
        fetcher = OpenAIFetcher()
        outcome = fetcher._convert_entry(feed.entries[0], _SOURCE_ID, "en")
        assert isinstance(outcome, PendingHtmlFetch)
        assert "Product" in outcome.metadata.tags
