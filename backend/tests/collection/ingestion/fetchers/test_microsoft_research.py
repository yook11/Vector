"""``MicrosoftResearchFetcher`` の単体テスト (Phase 1c-A2)。

per-source 設計:
- body は ``content[0].value`` 直取り **+ 末尾 footer regex strip**
- ``entry.author`` (comma-separated) を ``authors`` tuple に分解
- image_url は構造的に **常に None** (RSS が <media:content> を提供しない)
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import feedparser

from app.collection.ingestion.domain.fetched_article import (
    Failed,
    ReadyForArticle,
)
from app.collection.ingestion.fetchers.microsoft_research import (
    MicrosoftResearchFetcher,
    _extract_authors_from_csv,
    _strip_footer,
)
from app.models.news_source import NewsSource

_FIXTURE = (
    Path(__file__).parent.parent.parent.parent
    / "fixtures"
    / "microsoft_research_rss.xml"
)


def _source(source_id: int = 1, name: str = "Microsoft Research") -> NewsSource:
    s = MagicMock(spec=NewsSource)
    s.id = source_id
    s.name = name
    s.endpoint_url = "https://www.microsoft.com/en-us/research/feed/"
    return s


_LOREM = "Lorem ipsum dolor sit amet, consectetur adipiscing elit. "
_FOOTER = (
    " Opens in a new tab The post Some Title appeared first on Microsoft Research."
)


def _entry(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "title": "MR Title",
        "link": "https://www.microsoft.com/en-us/research/blog/article/",
        "id": "https://www.microsoft.com/en-us/research/?p=1",
        "summary": "<p>short snippet</p>",
        "content": [
            {
                "value": (
                    "<p>"
                    + _LOREM * 10
                    + "</p>"
                    + '<a href="#">Opens in a new tab</a>'
                    + "The post MR Title appeared first on Microsoft Research."
                )
            }
        ],
        "published_parsed": time.struct_time((2026, 4, 30, 12, 0, 0, 0, 0, 0)),
        "author": "Sidharth Sinha, Anson Bastos, Xuchao Zhang",
        "tags": [{"term": "Research Blog"}],
    }
    base.update(overrides)
    return base


class TestProvides:
    def test_provides_minimum_set(self) -> None:
        assert MicrosoftResearchFetcher.PROVIDES == frozenset(
            {"language", "guid", "site_name"}
        )


class TestStripFooter:
    def test_removes_footer_at_end(self) -> None:
        body = "Some content here." + _FOOTER
        result = _strip_footer(body)
        assert "appeared first on Microsoft Research" not in result
        assert result == "Some content here."

    def test_no_op_when_no_footer(self) -> None:
        body = "Some content without the footer marker."
        assert _strip_footer(body) == body

    def test_handles_extra_whitespace(self) -> None:
        body = (
            "Body text.   Opens in a new tab   The post X "
            "appeared first on Microsoft Research."
        )
        result = _strip_footer(body)
        assert "appeared first" not in result


class TestExtractAuthorsFromCsv:
    def test_splits_comma_separated(self) -> None:
        assert _extract_authors_from_csv("Alice, Bob, Charlie") == (
            "Alice",
            "Bob",
            "Charlie",
        )

    def test_strips_whitespace(self) -> None:
        assert _extract_authors_from_csv("  Alice  ,Bob ,  Charlie") == (
            "Alice",
            "Bob",
            "Charlie",
        )

    def test_skips_empty(self) -> None:
        assert _extract_authors_from_csv("Alice,,Bob") == ("Alice", "Bob")

    def test_single_author(self) -> None:
        assert _extract_authors_from_csv("Alice") == ("Alice",)

    def test_none_returns_empty(self) -> None:
        assert _extract_authors_from_csv(None) == ()

    def test_empty_returns_empty(self) -> None:
        assert _extract_authors_from_csv("") == ()


class TestConvertEntry:
    def setup_method(self) -> None:
        self.fetcher = MicrosoftResearchFetcher()
        self.source = _source()

    def test_image_url_is_always_none(self) -> None:
        # 構造的事実: MR RSS には <media:content> が無い。
        # たとえ entry に media_content が混入しても拾わない設計。
        outcome = self.fetcher._convert_entry(
            _entry(media_content=[{"url": "https://example.com/x.jpg"}]),
            self.source,
            "en-US",
        )
        assert isinstance(outcome, ReadyForArticle)
        assert outcome.metadata.image_url is None

    def test_footer_is_stripped_from_body(self) -> None:
        outcome = self.fetcher._convert_entry(_entry(), self.source, "en-US")
        assert isinstance(outcome, ReadyForArticle)
        assert "appeared first on Microsoft Research" not in outcome.article.body

    def test_authors_tuple_split_from_csv(self) -> None:
        outcome = self.fetcher._convert_entry(_entry(), self.source, "en-US")
        assert isinstance(outcome, ReadyForArticle)
        assert outcome.metadata.authors == (
            "Sidharth Sinha",
            "Anson Bastos",
            "Xuchao Zhang",
        )

    def test_author_field_holds_raw_string(self) -> None:
        outcome = self.fetcher._convert_entry(_entry(), self.source, "en-US")
        assert isinstance(outcome, ReadyForArticle)
        # raw comma-separated を 200 chars cap で詰める
        assert outcome.metadata.author == "Sidharth Sinha, Anson Bastos, Xuchao Zhang"

    def test_extracts_tags(self) -> None:
        outcome = self.fetcher._convert_entry(_entry(), self.source, "en-US")
        assert isinstance(outcome, ReadyForArticle)
        assert outcome.metadata.tags == ("Research Blog",)

    def test_empty_title_returns_failed(self) -> None:
        outcome = self.fetcher._convert_entry(_entry(title=""), self.source, "en-US")
        assert isinstance(outcome, Failed)
        assert outcome.reason.code == "title_missing"

    def test_short_body_returns_failed(self) -> None:
        outcome = self.fetcher._convert_entry(
            _entry(content=[{"value": "<p>tiny</p>"}]), self.source, "en-US"
        )
        assert isinstance(outcome, Failed)
        assert outcome.reason.code == "body_too_short"

    def test_invalid_link_returns_failed(self) -> None:
        outcome = self.fetcher._convert_entry(
            _entry(link="not-a-url"), self.source, "en-US"
        )
        assert isinstance(outcome, Failed)
        assert outcome.reason.code == "extraction_empty"


class TestFixtureParsing:
    def test_fixture_first_entry_strips_footer(self) -> None:
        text = _FIXTURE.read_text(encoding="utf-8")
        feed = feedparser.parse(text)
        fetcher = MicrosoftResearchFetcher()
        outcome = fetcher._convert_entry(feed.entries[0], _source(), "en-US")
        assert isinstance(outcome, ReadyForArticle)
        assert outcome.article.title.startswith("Scaling graph foundation models")
        assert "graph foundation models" in outcome.article.body
        # footer が body から除去されている
        assert "appeared first on Microsoft Research" not in outcome.article.body

    def test_fixture_first_entry_multi_author(self) -> None:
        text = _FIXTURE.read_text(encoding="utf-8")
        feed = feedparser.parse(text)
        fetcher = MicrosoftResearchFetcher()
        outcome = fetcher._convert_entry(feed.entries[0], _source(), "en-US")
        assert isinstance(outcome, ReadyForArticle)
        assert outcome.metadata.authors == (
            "Sidharth Sinha",
            "Anson Bastos",
            "Xuchao Zhang",
        )
        assert outcome.metadata.image_url is None  # RSS に <media:content> なし

    def test_fixture_second_entry_too_short_yields_failed(self) -> None:
        text = _FIXTURE.read_text(encoding="utf-8")
        feed = feedparser.parse(text)
        fetcher = MicrosoftResearchFetcher()
        outcome = fetcher._convert_entry(feed.entries[1], _source(), "en-US")
        assert isinstance(outcome, Failed)
        assert outcome.reason.code == "body_too_short"
