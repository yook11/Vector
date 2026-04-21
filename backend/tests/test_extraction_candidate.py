"""extraction/candidate.py の VO とファクトリ・invariant のユニットテスト。"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.collection.extraction.candidate import (
    ArticleExtractedContent,
    PublishedAt,
)
from app.collection.extraction.extractor import HtmlExtractionResult


class TestPublishedAtParse:
    def test_parse_iso_datetime_assigns_utc(self) -> None:
        published = PublishedAt.parse("2026-04-01T12:34:56")
        assert published is not None
        assert published.value == datetime(2026, 4, 1, 12, 34, 56, tzinfo=UTC)

    def test_parse_date_only_assigns_utc_midnight(self) -> None:
        published = PublishedAt.parse("2026-04-01")
        assert published is not None
        assert published.value == datetime(2026, 4, 1, tzinfo=UTC)

    def test_parse_returns_none_for_empty(self) -> None:
        assert PublishedAt.parse(None) is None
        assert PublishedAt.parse("") is None

    def test_parse_returns_none_for_unknown_format(self) -> None:
        assert PublishedAt.parse("April 1, 2026") is None


class TestPublishedAtInvariant:
    def test_rejects_naive_datetime(self) -> None:
        with pytest.raises(ValueError, match="timezone-aware"):
            PublishedAt(datetime(2026, 4, 1, 12, 0, 0))


class TestArticleExtractedContentFromExtraction:
    def test_builds_when_title_and_body_present(self) -> None:
        body = "x" * 60
        result = HtmlExtractionResult(
            body=body,
            title="A valid title",
            published_at=datetime(2026, 4, 1, tzinfo=UTC),
        )

        content = ArticleExtractedContent.from_extraction(result)

        assert content is not None
        assert content.title == "A valid title"
        assert content.body == body
        assert content.published_at is not None
        assert content.published_at.value == datetime(2026, 4, 1, tzinfo=UTC)

    def test_builds_without_published_at(self) -> None:
        result = HtmlExtractionResult(body="x" * 60, title="t", published_at=None)

        content = ArticleExtractedContent.from_extraction(result)

        assert content is not None
        assert content.published_at is None

    def test_returns_none_when_title_missing(self) -> None:
        result = HtmlExtractionResult(body="x" * 60, title=None, published_at=None)
        assert ArticleExtractedContent.from_extraction(result) is None

    def test_returns_none_when_body_missing(self) -> None:
        result = HtmlExtractionResult(body=None, title="t", published_at=None)
        assert ArticleExtractedContent.from_extraction(result) is None


class TestArticleExtractedContentInvariant:
    def test_rejects_empty_title(self) -> None:
        with pytest.raises(ValueError, match="title"):
            ArticleExtractedContent(title="", body="x" * 60, published_at=None)

    def test_rejects_short_body(self) -> None:
        with pytest.raises(ValueError, match="body"):
            ArticleExtractedContent(title="t", body="x" * 10, published_at=None)
