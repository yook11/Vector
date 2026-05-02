"""``KrebsOnSecurityFetcher`` の単体テスト (Phase 1c-A1)。

per-source 設計:
- author / language は取得、image は構造的に **常に None**
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
from app.collection.ingestion.fetchers.krebs_on_security import (
    KrebsOnSecurityFetcher,
    _extract_body,
)

_FIXTURE = (
    Path(__file__).parent.parent.parent.parent
    / "fixtures"
    / "krebs_on_security_rss.xml"
)


_SOURCE_ID = 1


_LOREM = "Lorem ipsum dolor sit amet, consectetur adipiscing elit. "


def _entry(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "title": "Krebs Title",
        "link": "https://krebsonsecurity.com/article/",
        "id": "https://krebsonsecurity.com/?p=1",
        "summary": "<p>" + _LOREM + "</p>",
        "content": [{"value": "<p>" + _LOREM * 10 + "</p>"}],
        "published_parsed": time.struct_time((2026, 4, 30, 12, 0, 0, 0, 0, 0)),
        "author": "BrianKrebs",
        "tags": [{"term": "Phishing"}],
    }
    base.update(overrides)
    return base


class TestProvides:
    def test_provides_minimum_set(self) -> None:
        assert KrebsOnSecurityFetcher.PROVIDES == frozenset(
            {"language", "guid", "site_name"}
        )


class TestExtractBody:
    def test_takes_content_encoded_directly(self) -> None:
        entry = {"content": [{"value": "full body"}]}
        assert _extract_body(entry) == "full body"


class TestConvertEntry:
    def setup_method(self) -> None:
        self.fetcher = KrebsOnSecurityFetcher()
        self.source_id = _SOURCE_ID

    def test_image_url_is_always_none(self) -> None:
        # 構造的事実: Krebs RSS には <media:content> が無い。
        # たとえ entry に media_content が混入しても拾わない設計。
        outcome = self.fetcher._convert_entry(
            _entry(media_content=[{"url": "https://example.com/foo.jpg"}]),
            self.source_id,
            "en-US",
        )
        assert isinstance(outcome, ReadyForArticle)
        assert outcome.metadata.image_url is None

    def test_extracts_author(self) -> None:
        outcome = self.fetcher._convert_entry(_entry(), self.source_id, "en-US")
        assert isinstance(outcome, ReadyForArticle)
        assert outcome.metadata.author == "BrianKrebs"

    def test_extracts_tags(self) -> None:
        outcome = self.fetcher._convert_entry(_entry(), self.source_id, "en-US")
        assert isinstance(outcome, ReadyForArticle)
        assert outcome.metadata.tags == ("Phishing",)

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

    def test_invalid_link_returns_failed(self) -> None:
        outcome = self.fetcher._convert_entry(
            _entry(link="not-a-url"), self.source_id, "en-US"
        )
        assert isinstance(outcome, Failed)
        assert outcome.reason.code == "extraction_empty"


class TestFixtureParsing:
    def test_fixture_first_entry_yields_ready_without_image(self) -> None:
        text = _FIXTURE.read_text(encoding="utf-8")
        feed = feedparser.parse(text)
        fetcher = KrebsOnSecurityFetcher()
        outcome = fetcher._convert_entry(feed.entries[0], _SOURCE_ID, "en-US")
        assert isinstance(outcome, ReadyForArticle)
        assert outcome.article.title.startswith("Phishing campaign abuses")
        assert "phishing kits" in outcome.article.body
        assert outcome.metadata.author == "BrianKrebs"
        assert outcome.metadata.image_url is None  # fixture に <media:content> なし
        assert "Phishing" in outcome.metadata.tags

    def test_fixture_second_entry_too_short_yields_failed(self) -> None:
        text = _FIXTURE.read_text(encoding="utf-8")
        feed = feedparser.parse(text)
        fetcher = KrebsOnSecurityFetcher()
        outcome = fetcher._convert_entry(feed.entries[1], _SOURCE_ID, "en-US")
        assert isinstance(outcome, Failed)
        assert outcome.reason.code == "body_too_short"
