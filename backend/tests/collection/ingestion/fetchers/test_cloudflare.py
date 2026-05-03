"""``CloudflareBlogFetcher`` の単体テスト (Phase 3 PR 3-d-1)。

per-source 設計:
- RSS 2.0 + dc/content/atom/media namespaces
- ``<content:encoded>`` を full body source として直取り (Pattern R)
- ``<dc:creator>`` 多重 → ``metadata.authors`` (tuple)、先頭は
  ``metadata.author`` にも duplicate
- ``<media:content>`` は提供されないため image_url は None
- PROVIDES = {language, guid, site_name}
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import feedparser

from app.collection.ingestion.domain.fetched_article import (
    Failed,
    ReadyForArticle,
)
from app.collection.ingestion.fetchers.cloudflare import CloudflareBlogFetcher

_FIXTURE = (
    Path(__file__).parent.parent.parent.parent / "fixtures" / "cloudflare_rss.xml"
)

_SOURCE_ID = 1

_LOREM = "Lorem ipsum dolor sit amet, consectetur adipiscing elit. "


def _entry(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "title": "Cloudflare Test Title",
        "link": "https://blog.cloudflare.com/test-article/",
        "id": "abc123hashid",
        "authors": [{"name": "Jane Doe"}],
        "content": [{"value": "<p>" + _LOREM * 5 + "</p>"}],
        "summary": "short description",
        "published_parsed": time.struct_time((2026, 5, 1, 12, 0, 0, 0, 0, 0)),
        "tags": [{"term": "Networking"}, {"term": "Security"}],
    }
    base.update(overrides)
    return base


class TestProvides:
    def test_provides_minimum_set(self) -> None:
        assert CloudflareBlogFetcher.PROVIDES == frozenset(
            {"language", "guid", "site_name"}
        )

    def test_endpoint_is_canonical_rss(self) -> None:
        assert CloudflareBlogFetcher.ENDPOINT_URL == "https://blog.cloudflare.com/rss/"


class TestConvertEntry:
    def setup_method(self) -> None:
        self.fetcher = CloudflareBlogFetcher()
        self.source_id = _SOURCE_ID

    def test_valid_entry_yields_ready(self) -> None:
        outcome = self.fetcher._convert_entry(_entry(), self.source_id, "en-US")
        assert isinstance(outcome, ReadyForArticle)
        assert outcome.article.title == "Cloudflare Test Title"

    def test_body_extracted_from_content_encoded(self) -> None:
        outcome = self.fetcher._convert_entry(_entry(), self.source_id, "en-US")
        assert isinstance(outcome, ReadyForArticle)
        assert "Lorem ipsum" in outcome.article.body
        assert len(outcome.article.body) >= 50

    def test_empty_title_returns_failed(self) -> None:
        outcome = self.fetcher._convert_entry(_entry(title=""), self.source_id, "en-US")
        assert isinstance(outcome, Failed)
        assert outcome.reason.code == "title_missing"

    def test_short_body_returns_failed(self) -> None:
        outcome = self.fetcher._convert_entry(
            _entry(content=[{"value": "<p>tiny</p>"}]), self.source_id, "en-US"
        )
        assert isinstance(outcome, Failed)
        assert outcome.reason.code == "body_too_short"

    def test_missing_pubdate_returns_failed(self) -> None:
        # Pattern R では published_at 必須
        entry = _entry()
        del entry["published_parsed"]
        outcome = self.fetcher._convert_entry(entry, self.source_id, "en-US")
        assert isinstance(outcome, Failed)
        assert outcome.reason.code == "published_at_missing"

    def test_invalid_link_returns_failed(self) -> None:
        outcome = self.fetcher._convert_entry(
            _entry(link="not-a-url"), self.source_id, "en-US"
        )
        assert isinstance(outcome, Failed)
        assert outcome.reason.code == "extraction_empty"

    def test_single_author_in_authors_tuple(self) -> None:
        outcome = self.fetcher._convert_entry(_entry(), self.source_id, "en-US")
        assert isinstance(outcome, ReadyForArticle)
        assert outcome.metadata.authors == ("Jane Doe",)
        assert outcome.metadata.author == "Jane Doe"

    def test_multi_author_preserved_in_authors_tuple(self) -> None:
        outcome = self.fetcher._convert_entry(
            _entry(authors=[{"name": "Alice"}, {"name": "Bob"}]),
            self.source_id,
            "en-US",
        )
        assert isinstance(outcome, ReadyForArticle)
        assert outcome.metadata.authors == ("Alice", "Bob")
        assert outcome.metadata.author == "Alice"

    def test_duplicate_authors_deduplicated(self) -> None:
        outcome = self.fetcher._convert_entry(
            _entry(authors=[{"name": "Alice"}, {"name": "Alice"}]),
            self.source_id,
            "en-US",
        )
        assert isinstance(outcome, ReadyForArticle)
        assert outcome.metadata.authors == ("Alice",)

    def test_no_authors_yields_empty_tuple_and_none(self) -> None:
        outcome = self.fetcher._convert_entry(
            _entry(authors=None), self.source_id, "en-US"
        )
        assert isinstance(outcome, ReadyForArticle)
        assert outcome.metadata.authors == ()
        assert outcome.metadata.author is None

    def test_extracts_tags(self) -> None:
        outcome = self.fetcher._convert_entry(_entry(), self.source_id, "en-US")
        assert isinstance(outcome, ReadyForArticle)
        assert outcome.metadata.tags == ("Networking", "Security")

    def test_image_url_hardcoded_none(self) -> None:
        # Cloudflare RSS は <media:content> を提供しない
        outcome = self.fetcher._convert_entry(_entry(), self.source_id, "en-US")
        assert isinstance(outcome, ReadyForArticle)
        assert outcome.metadata.image_url is None

    def test_extracts_guid_from_id(self) -> None:
        outcome = self.fetcher._convert_entry(_entry(), self.source_id, "en-US")
        assert isinstance(outcome, ReadyForArticle)
        assert outcome.metadata.guid == "abc123hashid"

    def test_language_passthrough(self) -> None:
        outcome = self.fetcher._convert_entry(_entry(), self.source_id, "en-US")
        assert isinstance(outcome, ReadyForArticle)
        assert outcome.metadata.language == "en-US"

    def test_site_name_hardcoded(self) -> None:
        outcome = self.fetcher._convert_entry(_entry(), self.source_id, "en-US")
        assert isinstance(outcome, ReadyForArticle)
        assert outcome.metadata.site_name == "The Cloudflare Blog"


class TestFixtureParsing:
    def test_fixture_parses_three_entries(self) -> None:
        feed = feedparser.parse(_FIXTURE.read_bytes())
        assert len(feed.entries) == 3

    def test_fixture_first_entry_is_pattern_r_ready(self) -> None:
        feed = feedparser.parse(_FIXTURE.read_bytes())
        fetcher = CloudflareBlogFetcher()
        outcome = fetcher._convert_entry(feed.entries[0], _SOURCE_ID, "en-us")
        assert isinstance(outcome, ReadyForArticle)
        assert outcome.article.title.startswith("Code Orange")
        assert "Lorem ipsum" in outcome.article.body
        assert outcome.metadata.author == "Jeremy Hartman"
        assert outcome.metadata.authors == ("Jeremy Hartman",)

    def test_fixture_second_entry_has_multi_authors(self) -> None:
        feed = feedparser.parse(_FIXTURE.read_bytes())
        fetcher = CloudflareBlogFetcher()
        outcome = fetcher._convert_entry(feed.entries[1], _SOURCE_ID, "en-us")
        assert isinstance(outcome, ReadyForArticle)
        assert outcome.metadata.authors == ("Dan Lapid", "Luís Duarte")
        assert outcome.metadata.author == "Dan Lapid"

    def test_fixture_first_entry_guid_is_short_hash(self) -> None:
        feed = feedparser.parse(_FIXTURE.read_bytes())
        fetcher = CloudflareBlogFetcher()
        outcome = fetcher._convert_entry(feed.entries[0], _SOURCE_ID, "en-us")
        assert isinstance(outcome, ReadyForArticle)
        assert outcome.metadata.guid == "6EfXlJEx6OJ21w9NlnS59D"
