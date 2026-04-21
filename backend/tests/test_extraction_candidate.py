"""extraction/candidate.py の VO・invariant のユニットテスト。"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.collection.extraction.candidate import PublishedAt


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
