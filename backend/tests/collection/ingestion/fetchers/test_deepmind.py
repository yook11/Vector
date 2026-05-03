"""``DeepMindFetcher`` の単体テスト (Phase 3 PR 3-d-1)。

per-source 設計:
- RSS 2.0 + atom/media namespaces
- ``<description>`` のみ (短い概要、< 200 chars)、本文は HTML 抽出に委譲 → Pattern H
- ``<author>`` / ``<dc:creator>`` 未提供 → ``metadata.author = "Google DeepMind"``
  hardcode (PROVIDES に "author" を含む)
- ``<media:thumbnail>`` を ``metadata.image_url`` に詰める (probabilistic)
- ``<guid>`` は link と同値の絶対 URL
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
from app.collection.ingestion.fetchers.deepmind import DeepMindFetcher

_FIXTURE = Path(__file__).parent.parent.parent.parent / "fixtures" / "deepmind_rss.xml"

_SOURCE_ID = 1


def _entry(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "title": "DeepMind Test Article",
        "link": "https://deepmind.google/blog/test-article/",
        "id": "https://deepmind.google/blog/test-article/",
        "published_parsed": time.struct_time((2026, 4, 30, 12, 0, 0, 0, 0, 0)),
        "media_thumbnail": [{"url": "https://example.com/thumb.png"}],
    }
    base.update(overrides)
    return base


class TestProvides:
    def test_provides_includes_author(self) -> None:
        # author hardcode のため PROVIDES に含める
        assert DeepMindFetcher.PROVIDES == frozenset(
            {"language", "guid", "site_name", "author"}
        )

    def test_endpoint_is_canonical_rss(self) -> None:
        assert DeepMindFetcher.ENDPOINT_URL == "https://deepmind.google/blog/rss.xml"


class TestConvertEntry:
    def setup_method(self) -> None:
        self.fetcher = DeepMindFetcher()
        self.source_id = _SOURCE_ID

    def test_valid_entry_yields_pending(self) -> None:
        outcome = self.fetcher._convert_entry(_entry(), self.source_id, "en")
        assert isinstance(outcome, PendingHtmlFetch)
        assert outcome.title == "DeepMind Test Article"

    def test_does_not_construct_body(self) -> None:
        outcome = self.fetcher._convert_entry(_entry(), self.source_id, "en")
        assert isinstance(outcome, PendingHtmlFetch)
        assert not hasattr(outcome, "body")

    def test_empty_title_returns_failed(self) -> None:
        outcome = self.fetcher._convert_entry(_entry(title=""), self.source_id, "en")
        assert isinstance(outcome, Failed)
        assert outcome.reason.code == "title_missing"

    def test_invalid_link_returns_failed(self) -> None:
        outcome = self.fetcher._convert_entry(
            _entry(link="not-a-url"), self.source_id, "en"
        )
        assert isinstance(outcome, Failed)
        assert outcome.reason.code == "extraction_empty"

    def test_missing_pubdate_yields_pending_with_none_hint(self) -> None:
        # Pattern H: 緩い品質ゲート、HTML 補完を待つ
        entry = _entry()
        del entry["published_parsed"]
        outcome = self.fetcher._convert_entry(entry, self.source_id, "en")
        assert isinstance(outcome, PendingHtmlFetch)
        assert outcome.published_at_hint is None

    def test_metadata_author_hardcoded(self) -> None:
        outcome = self.fetcher._convert_entry(_entry(), self.source_id, "en")
        assert isinstance(outcome, PendingHtmlFetch)
        assert outcome.metadata.author == "Google DeepMind"

    def test_metadata_image_url_from_media_thumbnail(self) -> None:
        outcome = self.fetcher._convert_entry(_entry(), self.source_id, "en")
        assert isinstance(outcome, PendingHtmlFetch)
        assert outcome.metadata.image_url is not None
        assert "example.com/thumb.png" in str(outcome.metadata.image_url.root)

    def test_image_url_falls_back_to_media_content(self) -> None:
        entry = _entry()
        del entry["media_thumbnail"]
        entry["media_content"] = [
            {"medium": "image", "url": "https://example.com/from-content.png"}
        ]
        outcome = self.fetcher._convert_entry(entry, self.source_id, "en")
        assert isinstance(outcome, PendingHtmlFetch)
        assert outcome.metadata.image_url is not None
        assert "from-content.png" in str(outcome.metadata.image_url.root)

    def test_image_url_none_when_no_media(self) -> None:
        entry = _entry()
        del entry["media_thumbnail"]
        outcome = self.fetcher._convert_entry(entry, self.source_id, "en")
        assert isinstance(outcome, PendingHtmlFetch)
        assert outcome.metadata.image_url is None

    def test_extracts_guid_from_id(self) -> None:
        outcome = self.fetcher._convert_entry(_entry(), self.source_id, "en")
        assert isinstance(outcome, PendingHtmlFetch)
        assert outcome.metadata.guid == "https://deepmind.google/blog/test-article/"

    def test_language_passthrough(self) -> None:
        outcome = self.fetcher._convert_entry(_entry(), self.source_id, "en")
        assert isinstance(outcome, PendingHtmlFetch)
        assert outcome.metadata.language == "en"

    def test_site_name_hardcoded(self) -> None:
        outcome = self.fetcher._convert_entry(_entry(), self.source_id, "en")
        assert isinstance(outcome, PendingHtmlFetch)
        assert outcome.metadata.site_name == "Google DeepMind"


class TestFixtureParsing:
    def test_fixture_parses_three_entries(self) -> None:
        feed = feedparser.parse(_FIXTURE.read_bytes())
        assert len(feed.entries) == 3

    def test_fixture_first_entry_is_pattern_h_pending(self) -> None:
        feed = feedparser.parse(_FIXTURE.read_bytes())
        fetcher = DeepMindFetcher()
        outcome = fetcher._convert_entry(feed.entries[0], _SOURCE_ID, "en")
        assert isinstance(outcome, PendingHtmlFetch)
        assert "AI co-clinician" in outcome.title
        assert outcome.metadata.author == "Google DeepMind"
        assert outcome.metadata.site_name == "Google DeepMind"

    def test_fixture_first_entry_has_image_url(self) -> None:
        feed = feedparser.parse(_FIXTURE.read_bytes())
        fetcher = DeepMindFetcher()
        outcome = fetcher._convert_entry(feed.entries[0], _SOURCE_ID, "en")
        assert isinstance(outcome, PendingHtmlFetch)
        assert outcome.metadata.image_url is not None
        assert "googleusercontent.com" in str(outcome.metadata.image_url.root)

    def test_fixture_first_entry_link_is_absolute(self) -> None:
        feed = feedparser.parse(_FIXTURE.read_bytes())
        fetcher = DeepMindFetcher()
        outcome = fetcher._convert_entry(feed.entries[0], _SOURCE_ID, "en")
        assert isinstance(outcome, PendingHtmlFetch)
        assert str(outcome.source_url.root).startswith("https://deepmind.google/blog/")
