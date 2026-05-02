"""``SpaceflightNowFetcher`` の単体テスト (Phase 1c-A1)。

per-source 設計:
- author / image_url は構造的に **常に None** (RSS が提供しない)
- language は **hardcoded "en-US"** (channel に language なし)
- body は ``content[0].value`` 直取り
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
from app.collection.ingestion.fetchers.spaceflight_now import (
    SpaceflightNowFetcher,
    _extract_body,
)

_FIXTURE = (
    Path(__file__).parent.parent.parent.parent / "fixtures" / "spaceflight_now_rss.xml"
)


_SOURCE_ID = 1


_LOREM = "Lorem ipsum dolor sit amet, consectetur adipiscing elit. "


def _entry(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "title": "Spaceflight Title",
        "link": "https://spaceflightnow.com/article/",
        "id": "https://spaceflightnow.com/article/",
        "content": [{"value": "<p>" + _LOREM * 10 + "</p>"}],
        "published_parsed": time.struct_time((2026, 4, 30, 12, 0, 0, 0, 0, 0)),
        "tags": [{"term": "Falcon 9"}],
    }
    base.update(overrides)
    return base


class TestProvides:
    def test_provides_minimum_set(self) -> None:
        assert SpaceflightNowFetcher.PROVIDES == frozenset(
            {"language", "guid", "site_name"}
        )


class TestExtractBody:
    def test_takes_content_encoded_directly(self) -> None:
        assert _extract_body({"content": [{"value": "full"}]}) == "full"


class TestConvertEntry:
    def setup_method(self) -> None:
        self.fetcher = SpaceflightNowFetcher()
        self.source_id = _SOURCE_ID

    def test_author_is_always_none(self) -> None:
        # RSS が byline を提供しないので、たとえ entry に author が入っていても拾わない
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
        # feed.feed.language を一切読まないので、引数 language を渡せない
        # ことを設計で明示。metadata.language は常に "en-US"。
        outcome = self.fetcher._convert_entry(_entry(), self.source_id)
        assert isinstance(outcome, ReadyForArticle)
        assert outcome.metadata.language == "en-US"

    def test_extracts_tags(self) -> None:
        outcome = self.fetcher._convert_entry(_entry(), self.source_id)
        assert isinstance(outcome, ReadyForArticle)
        assert outcome.metadata.tags == ("Falcon 9",)

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

    def test_invalid_link_returns_failed(self) -> None:
        outcome = self.fetcher._convert_entry(_entry(link="not-a-url"), self.source_id)
        assert isinstance(outcome, Failed)
        assert outcome.reason.code == "extraction_empty"


class TestFixtureParsing:
    def test_fixture_no_channel_language(self) -> None:
        text = _FIXTURE.read_text(encoding="utf-8")
        feed = feedparser.parse(text)
        # 構造的事実: channel に <language> がない
        assert feed.feed.get("language") is None

    def test_fixture_first_entry_yields_ready(self) -> None:
        text = _FIXTURE.read_text(encoding="utf-8")
        feed = feedparser.parse(text)
        fetcher = SpaceflightNowFetcher()
        outcome = fetcher._convert_entry(feed.entries[0], _SOURCE_ID)
        assert isinstance(outcome, ReadyForArticle)
        assert outcome.article.title.startswith("Falcon 9 launches")
        assert "Cape Canaveral" in outcome.article.body
        assert outcome.metadata.author is None
        assert outcome.metadata.image_url is None
        assert outcome.metadata.language == "en-US"  # hardcode

    def test_fixture_second_entry_too_short_yields_failed(self) -> None:
        text = _FIXTURE.read_text(encoding="utf-8")
        feed = feedparser.parse(text)
        fetcher = SpaceflightNowFetcher()
        outcome = fetcher._convert_entry(feed.entries[1], _SOURCE_ID)
        assert isinstance(outcome, Failed)
        assert outcome.reason.code == "body_too_short"
