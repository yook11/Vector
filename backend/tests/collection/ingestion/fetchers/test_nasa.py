"""``NASAFetcher`` の単体テスト (Phase 1c-A1)。

per-source 設計:
- author / image_url は構造的に **常に None**
- language は **hardcoded "en-US"**
- body は ``content[0].value`` 直取り (**nav noise 含むまま**)
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
from app.collection.ingestion.fetchers.nasa import NASAFetcher, _extract_body

_FIXTURE = Path(__file__).parent.parent.parent.parent / "fixtures" / "nasa_rss.xml"


_SOURCE_ID = 1


_LOREM = "Lorem ipsum dolor sit amet, consectetur adipiscing elit. "


def _entry(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "title": "NASA Title",
        "link": "https://www.nasa.gov/article/",
        "id": "https://www.nasa.gov/?p=1",
        "content": [{"value": "<p>" + _LOREM * 10 + "</p>"}],
        "published_parsed": time.struct_time((2026, 4, 30, 12, 0, 0, 0, 0, 0)),
        "tags": [{"term": "Astrophysics"}],
    }
    base.update(overrides)
    return base


class TestProvides:
    def test_provides_minimum_set(self) -> None:
        assert NASAFetcher.PROVIDES == frozenset({"language", "guid", "site_name"})


class TestExtractBody:
    def test_takes_content_encoded_directly(self) -> None:
        assert _extract_body({"content": [{"value": "full"}]}) == "full"


class TestConvertEntry:
    def setup_method(self) -> None:
        self.fetcher = NASAFetcher()
        self.source_id = _SOURCE_ID

    def test_author_is_always_none(self) -> None:
        outcome = self.fetcher._convert_entry(
            _entry(author="Should Not Appear"), self.source_id
        )
        assert isinstance(outcome, ReadyForArticle)
        assert outcome.metadata.author is None

    def test_image_url_is_always_none(self) -> None:
        outcome = self.fetcher._convert_entry(
            _entry(media_content=[{"url": "https://example.com/x.jpg"}]),
            self.source_id,
        )
        assert isinstance(outcome, ReadyForArticle)
        assert outcome.metadata.image_url is None

    def test_language_hardcoded_en_us(self) -> None:
        outcome = self.fetcher._convert_entry(_entry(), self.source_id)
        assert isinstance(outcome, ReadyForArticle)
        assert outcome.metadata.language == "en-US"

    def test_extracts_tags(self) -> None:
        outcome = self.fetcher._convert_entry(_entry(), self.source_id)
        assert isinstance(outcome, ReadyForArticle)
        assert outcome.metadata.tags == ("Astrophysics",)

    def test_empty_title_returns_failed(self) -> None:
        outcome = self.fetcher._convert_entry(_entry(title=""), self.source_id)
        assert isinstance(outcome, Failed)
        assert outcome.reason.code == "title_missing"

    def test_short_body_returns_failed(self) -> None:
        outcome = self.fetcher._convert_entry(
            _entry(content=[{"value": "<p>tiny</p>"}]), self.source_id
        )
        assert isinstance(outcome, Failed)
        assert outcome.reason.code == "body_too_short"


class TestFixtureParsing:
    def test_fixture_no_channel_language(self) -> None:
        text = _FIXTURE.read_text(encoding="utf-8")
        feed = feedparser.parse(text)
        assert feed.feed.get("language") is None

    def test_fixture_first_entry_includes_nav_noise_in_body(self) -> None:
        # 設計上の事実を test 化:
        # NASA の content:encoded には冒頭 nav menu と末尾 boilerplate が含まれ、
        # Phase 1 では受容して下流 (Stage 2 LLM) に渡す。
        text = _FIXTURE.read_text(encoding="utf-8")
        feed = feedparser.parse(text)
        fetcher = NASAFetcher()
        outcome = fetcher._convert_entry(feed.entries[0], _SOURCE_ID)
        assert isinstance(outcome, ReadyForArticle)
        assert "Earth Observatory" in outcome.article.body
        assert "Discover More Topics" in outcome.article.body

    def test_fixture_first_entry_metadata(self) -> None:
        text = _FIXTURE.read_text(encoding="utf-8")
        feed = feedparser.parse(text)
        fetcher = NASAFetcher()
        outcome = fetcher._convert_entry(feed.entries[0], _SOURCE_ID)
        assert isinstance(outcome, ReadyForArticle)
        assert outcome.metadata.author is None
        assert outcome.metadata.image_url is None
        assert outcome.metadata.language == "en-US"
        assert "Astrophysics" in outcome.metadata.tags

    def test_fixture_second_entry_too_short_yields_failed(self) -> None:
        text = _FIXTURE.read_text(encoding="utf-8")
        feed = feedparser.parse(text)
        fetcher = NASAFetcher()
        outcome = fetcher._convert_entry(feed.entries[1], _SOURCE_ID)
        assert isinstance(outcome, Failed)
        assert outcome.reason.code == "body_too_short"
