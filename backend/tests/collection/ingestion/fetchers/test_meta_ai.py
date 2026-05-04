"""``MetaAIFetcher`` の単体テスト (Phase 3 PR 3-d-3)。

per-source 設計:
- about.fb.com Newsroom RSS、Pattern R via content:encoded
- AI tag フィルタ business critical: tags に "AI" 含まないものは
  ``Failed(detail="not_ai_tagged")`` で drop
- ``<dc:creator>`` → author (大半 "Facebook" 固定)
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
from app.collection.ingestion.fetchers.meta_ai import (
    _AI_TAGS,
    MetaAIFetcher,
    _is_ai_tagged,
    _pick_body,
)

_FIXTURE = Path(__file__).parent.parent.parent.parent / "fixtures" / "meta_ai_rss.xml"

_SOURCE_ID = 1


_BODY = (
    "Meta announces new initiatives to power AI infrastructure with renewable "
    "energy from space-based solar arrays. " * 5
)


def _entry(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "title": "Meta AI Test Article",
        "link": "https://about.fb.com/news/2026/04/test/",
        "id": "https://about.fb.com/?p=48000",
        "summary": "<p>short summary</p>",
        "content": [{"value": "<p>" + _BODY + "</p>"}],
        "published_parsed": time.struct_time((2026, 4, 30, 13, 0, 0, 0, 0, 0)),
        "author": "Facebook",
        "tags": [{"term": "Meta"}, {"term": "AI"}, {"term": "Technology"}],
        "media_content": [
            {"url": "https://about.fb.com/wp-content/img1.jpg", "medium": "image"},
        ],
    }
    base.update(overrides)
    return base


class TestAITagFilter:
    def test_ai_tag_constant(self) -> None:
        # AI tag is the literal "AI" — case-sensitive match
        assert "AI" in _AI_TAGS

    def test_is_ai_tagged_yes(self) -> None:
        assert _is_ai_tagged(("Meta", "AI")) is True

    def test_is_ai_tagged_no(self) -> None:
        assert _is_ai_tagged(("Threads", "Product News")) is False

    def test_is_ai_tagged_empty(self) -> None:
        assert _is_ai_tagged(()) is False

    def test_is_ai_tagged_lowercase_ai_does_not_match(self) -> None:
        # 大文字小文字区別 (Newsroom 実フィードは "AI" で統一)
        assert _is_ai_tagged(("ai",)) is False


class TestPickBody:
    def test_prefers_longer_content(self) -> None:
        entry = {"summary": "short", "content": [{"value": "x" * 1000}]}
        assert _pick_body(entry) == "x" * 1000


class TestProvides:
    def test_provides_minimum_set(self) -> None:
        assert MetaAIFetcher.PROVIDES == frozenset({"language", "guid", "site_name"})

    def test_endpoint_is_about_fb(self) -> None:
        assert MetaAIFetcher.ENDPOINT_URL == "https://about.fb.com/news/feed/"


class TestConvertEntry:
    def setup_method(self) -> None:
        self.fetcher = MetaAIFetcher()

    def test_valid_ai_entry_yields_ready(self) -> None:
        outcome = self.fetcher._convert_entry(_entry(), _SOURCE_ID, "en-US")
        assert isinstance(outcome, ReadyForArticle)
        assert outcome.article.title == "Meta AI Test Article"
        assert "renewable energy" in outcome.article.body

    def test_non_ai_entry_drops(self) -> None:
        outcome = self.fetcher._convert_entry(
            _entry(tags=[{"term": "Threads"}, {"term": "Product News"}]),
            _SOURCE_ID,
            "en-US",
        )
        assert isinstance(outcome, Failed)
        assert outcome.reason.code == "other"
        assert outcome.reason.detail == "not_ai_tagged"

    def test_empty_tags_drops(self) -> None:
        outcome = self.fetcher._convert_entry(
            _entry(tags=[]),
            _SOURCE_ID,
            "en-US",
        )
        assert isinstance(outcome, Failed)
        assert outcome.reason.detail == "not_ai_tagged"

    def test_missing_tags_drops(self) -> None:
        e = _entry()
        del e["tags"]
        outcome = self.fetcher._convert_entry(e, _SOURCE_ID, "en-US")
        assert isinstance(outcome, Failed)
        assert outcome.reason.detail == "not_ai_tagged"

    def test_empty_title_returns_failed(self) -> None:
        outcome = self.fetcher._convert_entry(_entry(title=""), _SOURCE_ID, "en-US")
        assert isinstance(outcome, Failed)
        assert outcome.reason.code == "title_missing"

    def test_short_body_returns_failed(self) -> None:
        outcome = self.fetcher._convert_entry(
            _entry(summary="short", content=[{"value": "tiny"}]),
            _SOURCE_ID,
            "en-US",
        )
        assert isinstance(outcome, Failed)
        assert outcome.reason.code == "body_too_short"

    def test_missing_published_returns_failed(self) -> None:
        e = _entry()
        del e["published_parsed"]
        outcome = self.fetcher._convert_entry(e, _SOURCE_ID, "en-US")
        assert isinstance(outcome, Failed)
        assert outcome.reason.code == "published_at_missing"

    def test_invalid_link_returns_failed(self) -> None:
        outcome = self.fetcher._convert_entry(
            _entry(link="not-a-url"), _SOURCE_ID, "en-US"
        )
        assert isinstance(outcome, Failed)
        assert outcome.reason.code == "extraction_empty"

    def test_extracts_author(self) -> None:
        outcome = self.fetcher._convert_entry(_entry(), _SOURCE_ID, "en-US")
        assert isinstance(outcome, ReadyForArticle)
        assert outcome.metadata.author == "Facebook"

    def test_extracts_image_url(self) -> None:
        outcome = self.fetcher._convert_entry(_entry(), _SOURCE_ID, "en-US")
        assert isinstance(outcome, ReadyForArticle)
        assert outcome.metadata.image_url is not None
        assert "img1.jpg" in str(outcome.metadata.image_url)

    def test_tags_preserved_in_metadata(self) -> None:
        outcome = self.fetcher._convert_entry(_entry(), _SOURCE_ID, "en-US")
        assert isinstance(outcome, ReadyForArticle)
        assert outcome.metadata.tags == ("Meta", "AI", "Technology")

    def test_site_name_hardcoded(self) -> None:
        outcome = self.fetcher._convert_entry(_entry(), _SOURCE_ID, "en-US")
        assert isinstance(outcome, ReadyForArticle)
        assert outcome.metadata.site_name == "Meta AI"


class TestFixtureParsing:
    def test_fixture_parses_three_entries(self) -> None:
        feed = feedparser.parse(_FIXTURE.read_bytes())
        assert len(feed.entries) == 3

    def test_fixture_first_two_entries_are_ai(self) -> None:
        # AI tagged entries はそのまま ReadyForArticle に
        feed = feedparser.parse(_FIXTURE.read_bytes())
        fetcher = MetaAIFetcher()
        for i in (0, 1):
            outcome = fetcher._convert_entry(feed.entries[i], _SOURCE_ID, "en-US")
            assert isinstance(outcome, ReadyForArticle), f"entry {i}"

    def test_fixture_third_entry_threads_drops(self) -> None:
        # Threads / Product News のみ → AI フィルタで drop
        feed = feedparser.parse(_FIXTURE.read_bytes())
        fetcher = MetaAIFetcher()
        outcome = fetcher._convert_entry(feed.entries[2], _SOURCE_ID, "en-US")
        assert isinstance(outcome, Failed)
        assert outcome.reason.detail == "not_ai_tagged"
