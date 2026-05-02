"""``IEEESpectrumFetcher`` の単体テスト (Phase 1c-A2)。

per-source 設計:
- body source = ``entry.summary`` 直取り (``content[0]`` 空のため読まない)
- multi-author を ``FetchedMetadata.authors`` tuple に保持
- image / language / tags / guid は標準経路
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
from app.collection.ingestion.fetchers.ieee_spectrum import (
    IEEESpectrumFetcher,
    _extract_authors,
    _extract_body,
)

_FIXTURE = (
    Path(__file__).parent.parent.parent.parent / "fixtures" / "ieee_spectrum_rss.xml"
)


_SOURCE_ID = 1


_LOREM = "Lorem ipsum dolor sit amet, consectetur adipiscing elit. "


def _entry(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "title": "IEEE Title",
        "link": "https://spectrum.ieee.org/article/",
        "id": "https://spectrum.ieee.org/?p=1",
        "summary": "<p>" + _LOREM * 10 + "</p>",
        "content": [{"value": ""}],  # IEEE: content:encoded は空
        "published_parsed": time.struct_time((2026, 4, 30, 12, 0, 0, 0, 0, 0)),
        "authors": [{"name": "Olivia Hsu"}, {"name": "Kalhan Koul"}],
        "tags": [{"term": "Computing"}, {"term": "Photonics"}],
        "media_content": [
            {"url": "https://example.com/photonics.jpg", "medium": "image"}
        ],
    }
    base.update(overrides)
    return base


class TestProvides:
    def test_provides_minimum_set(self) -> None:
        assert IEEESpectrumFetcher.PROVIDES == frozenset(
            {"language", "guid", "site_name"}
        )


class TestExtractBody:
    def test_takes_summary_directly(self) -> None:
        # IEEE 設計: content:encoded を読まないことを構造保証する
        entry = {
            "summary": "summary body",
            "content": [{"value": "should not be picked"}],
        }
        assert _extract_body(entry) == "summary body"

    def test_no_content_fallback(self) -> None:
        # summary が無いとき content[0] にフォールバックしない
        assert _extract_body({"content": [{"value": "X"}]}) == ""

    def test_empty_when_missing(self) -> None:
        assert _extract_body({}) == ""


class TestExtractAuthors:
    def test_extracts_multiple_authors(self) -> None:
        entry = {"authors": [{"name": "Alice"}, {"name": "Bob"}]}
        assert _extract_authors(entry) == ("Alice", "Bob")

    def test_skips_invalid_entries(self) -> None:
        entry = {"authors": [{"name": "Alice"}, {}, {"name": ""}, "string"]}
        assert _extract_authors(entry) == ("Alice",)

    def test_empty_when_missing(self) -> None:
        assert _extract_authors({}) == ()


class TestConvertEntry:
    def setup_method(self) -> None:
        self.fetcher = IEEESpectrumFetcher()
        self.source_id = _SOURCE_ID

    def test_valid_entry_yields_ready(self) -> None:
        outcome = self.fetcher._convert_entry(_entry(), self.source_id, "en-US")
        assert isinstance(outcome, ReadyForArticle)
        assert outcome.article.title == "IEEE Title"
        assert "Lorem ipsum" in outcome.article.body
        assert outcome.metadata.language == "en-US"
        assert outcome.metadata.site_name == "IEEE Spectrum"

    def test_multi_author_in_authors_tuple(self) -> None:
        outcome = self.fetcher._convert_entry(_entry(), self.source_id, "en-US")
        assert isinstance(outcome, ReadyForArticle)
        assert outcome.metadata.authors == ("Olivia Hsu", "Kalhan Koul")

    def test_author_field_holds_first_author(self) -> None:
        outcome = self.fetcher._convert_entry(_entry(), self.source_id, "en-US")
        assert isinstance(outcome, ReadyForArticle)
        assert outcome.metadata.author == "Olivia Hsu"

    def test_no_authors_yields_none_and_empty_tuple(self) -> None:
        outcome = self.fetcher._convert_entry(
            _entry(authors=[]), self.source_id, "en-US"
        )
        assert isinstance(outcome, ReadyForArticle)
        assert outcome.metadata.author is None
        assert outcome.metadata.authors == ()

    def test_extracts_image_url(self) -> None:
        outcome = self.fetcher._convert_entry(_entry(), self.source_id, "en-US")
        assert isinstance(outcome, ReadyForArticle)
        assert outcome.metadata.image_url is not None
        assert str(outcome.metadata.image_url) == "https://example.com/photonics.jpg"

    def test_extracts_tags(self) -> None:
        outcome = self.fetcher._convert_entry(_entry(), self.source_id, "en-US")
        assert isinstance(outcome, ReadyForArticle)
        assert outcome.metadata.tags == ("Computing", "Photonics")

    def test_empty_title_returns_failed(self) -> None:
        outcome = self.fetcher._convert_entry(_entry(title=""), self.source_id, "en-US")
        assert isinstance(outcome, Failed)
        assert outcome.reason.code == "title_missing"

    def test_short_body_returns_failed(self) -> None:
        outcome = self.fetcher._convert_entry(
            _entry(summary="<p>tiny</p>"), self.source_id, "en-US"
        )
        assert isinstance(outcome, Failed)
        assert outcome.reason.code == "body_too_short"

    def test_content_does_not_rescue_short_summary(self) -> None:
        # IEEE 設計: content[0] に長文があっても採用しない
        outcome = self.fetcher._convert_entry(
            _entry(
                summary="<p>tiny</p>",
                content=[{"value": "<p>" + _LOREM * 10 + "</p>"}],
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

    def test_fixture_first_entry_yields_ready_with_multi_author(self) -> None:
        text = _FIXTURE.read_text(encoding="utf-8")
        feed = feedparser.parse(text)
        fetcher = IEEESpectrumFetcher()
        outcome = fetcher._convert_entry(feed.entries[0], _SOURCE_ID, "en-US")
        assert isinstance(outcome, ReadyForArticle)
        assert outcome.article.title.startswith("New silicon photonics")
        assert "silicon photonics chip" in outcome.article.body
        # 2 著者構造保証
        assert outcome.metadata.authors == ("Olivia Hsu", "Kalhan Koul")
        assert outcome.metadata.author == "Olivia Hsu"
        assert outcome.metadata.image_url is not None
        assert "Computing" in outcome.metadata.tags

    def test_fixture_second_entry_too_short_yields_failed(self) -> None:
        text = _FIXTURE.read_text(encoding="utf-8")
        feed = feedparser.parse(text)
        fetcher = IEEESpectrumFetcher()
        outcome = fetcher._convert_entry(feed.entries[1], _SOURCE_ID, "en-US")
        assert isinstance(outcome, Failed)
        assert outcome.reason.code == "body_too_short"
