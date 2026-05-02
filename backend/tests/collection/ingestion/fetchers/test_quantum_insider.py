"""``QuantumInsiderFetcher`` の単体テスト (Phase 1c-A1)。

per-source 設計:
- author / image / language を全て取得できるソース
- body は ``content[0].value`` 直取り (summary fallback なし)
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
from app.collection.ingestion.fetchers.quantum_insider import (
    QuantumInsiderFetcher,
    _extract_body,
    _normalize_language,
    _strip_html,
)

_FIXTURE = (
    Path(__file__).parent.parent.parent.parent / "fixtures" / "quantum_insider_rss.xml"
)


_SOURCE_ID = 1


_LOREM = "Lorem ipsum dolor sit amet, consectetur adipiscing elit. "


def _entry(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "title": "Quantum Title",
        "link": "https://thequantuminsider.com/article/",
        "id": "https://thequantuminsider.com/?p=1",
        "summary": "<p>short snippet</p>",
        "content": [{"value": "<p>" + _LOREM * 10 + "</p>"}],
        "published_parsed": time.struct_time((2026, 4, 30, 12, 0, 0, 0, 0, 0)),
        "author": "Alice Chen",
        "tags": [{"term": "Quantum"}, {"term": "Hardware"}],
        "media_content": [
            {"url": "https://example.com/quantum.jpg", "medium": "image"}
        ],
    }
    base.update(overrides)
    return base


class TestProvides:
    def test_provides_minimum_set(self) -> None:
        assert QuantumInsiderFetcher.PROVIDES == frozenset(
            {"language", "guid", "site_name"}
        )


class TestExtractBody:
    def test_takes_content_encoded_directly(self) -> None:
        entry = {
            "summary": "should not be picked",
            "content": [{"value": "full body content"}],
        }
        assert _extract_body(entry) == "full body content"

    def test_no_summary_fallback(self) -> None:
        # QI design: content 不在のときに summary に落ちない
        assert _extract_body({"summary": "abc"}) == ""

    def test_empty_when_missing(self) -> None:
        assert _extract_body({}) == ""


class TestNormalizeLanguage:
    def test_default_when_missing(self) -> None:
        assert _normalize_language(None) == "en-US"

    def test_underscore_to_hyphen(self) -> None:
        assert _normalize_language("en_US") == "en-US"


class TestConvertEntry:
    def setup_method(self) -> None:
        self.fetcher = QuantumInsiderFetcher()
        self.source_id = _SOURCE_ID

    def test_valid_entry_yields_ready(self) -> None:
        outcome = self.fetcher._convert_entry(_entry(), self.source_id, "en-US")
        assert isinstance(outcome, ReadyForArticle)
        assert outcome.article.title == "Quantum Title"
        assert "Lorem ipsum" in outcome.article.body
        assert outcome.metadata.language == "en-US"
        assert outcome.metadata.site_name == "The Quantum Insider"
        assert outcome.metadata.guid == "https://thequantuminsider.com/?p=1"

    def test_extracts_author(self) -> None:
        outcome = self.fetcher._convert_entry(_entry(), self.source_id, "en-US")
        assert isinstance(outcome, ReadyForArticle)
        assert outcome.metadata.author == "Alice Chen"

    def test_extracts_image_url(self) -> None:
        outcome = self.fetcher._convert_entry(_entry(), self.source_id, "en-US")
        assert isinstance(outcome, ReadyForArticle)
        assert outcome.metadata.image_url is not None
        assert str(outcome.metadata.image_url) == "https://example.com/quantum.jpg"

    def test_extracts_tags(self) -> None:
        outcome = self.fetcher._convert_entry(_entry(), self.source_id, "en-US")
        assert isinstance(outcome, ReadyForArticle)
        assert outcome.metadata.tags == ("Quantum", "Hardware")

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

    def test_summary_does_not_rescue_short_body(self) -> None:
        # QI 設計: summary 側に長文があっても採用しない
        outcome = self.fetcher._convert_entry(
            _entry(
                summary="<p>" + _LOREM * 10 + "</p>",
                content=[{"value": "<p>tiny</p>"}],
            ),
            self.source_id,
            "en-US",
        )
        assert isinstance(outcome, Failed)
        assert outcome.reason.code == "body_too_short"

    def test_missing_published_returns_failed(self) -> None:
        e = _entry()
        del e["published_parsed"]
        outcome = self.fetcher._convert_entry(e, self.source_id, "en-US")
        assert isinstance(outcome, Failed)
        assert outcome.reason.code == "published_at_missing"

    def test_invalid_link_returns_failed(self) -> None:
        outcome = self.fetcher._convert_entry(
            _entry(link="not-a-url"), self.source_id, "en-US"
        )
        assert isinstance(outcome, Failed)
        assert outcome.reason.code == "extraction_empty"


class TestFixtureParsing:
    def test_fixture_parseable(self) -> None:
        text = _FIXTURE.read_text(encoding="utf-8")
        feed = feedparser.parse(text)
        assert not feed.bozo or feed.entries
        assert len(feed.entries) == 2
        assert feed.feed.get("language") == "en-US"

    def test_fixture_first_entry_yields_ready_with_image(self) -> None:
        text = _FIXTURE.read_text(encoding="utf-8")
        feed = feedparser.parse(text)
        fetcher = QuantumInsiderFetcher()
        outcome = fetcher._convert_entry(feed.entries[0], _SOURCE_ID, "en-US")
        assert isinstance(outcome, ReadyForArticle)
        assert outcome.article.title.startswith("Quantum hardware startup")
        assert "surface-code error correction" in outcome.article.body
        assert outcome.metadata.author == "Alice Chen"
        assert outcome.metadata.image_url is not None
        assert "Quantum Computing" in outcome.metadata.tags

    def test_fixture_second_entry_too_short_yields_failed(self) -> None:
        text = _FIXTURE.read_text(encoding="utf-8")
        feed = feedparser.parse(text)
        fetcher = QuantumInsiderFetcher()
        outcome = fetcher._convert_entry(feed.entries[1], _SOURCE_ID, "en-US")
        assert isinstance(outcome, Failed)
        assert outcome.reason.code == "body_too_short"


class TestStripHtml:
    def test_strips_tags(self) -> None:
        assert _strip_html("<p>hello</p>") == "hello"
