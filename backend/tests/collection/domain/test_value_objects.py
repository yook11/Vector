"""``collection.domain.value_objects`` のユニットテスト (DB 不要)。

``PublishedAt`` VO の parse 規則と TZ 不変条件を検証する。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.collection.domain.value_objects import PublishedAt

# PublishedAt — parse / invariant


class TestPublishedAtParse:
    def test_parse_iso_datetime_assigns_utc(self) -> None:
        published = PublishedAt.parse("2026-04-01T12:34:56")
        assert published is not None
        assert published.value == datetime(2026, 4, 1, 12, 34, 56, tzinfo=UTC)

    def test_parse_date_only_assigns_utc_midnight(self) -> None:
        published = PublishedAt.parse("2026-04-01")
        assert published is not None
        assert published.value == datetime(2026, 4, 1, tzinfo=UTC)

    def test_parse_returns_none_for_none(self) -> None:
        assert PublishedAt.parse(None) is None

    def test_parse_returns_none_for_empty_string(self) -> None:
        assert PublishedAt.parse("") is None

    def test_parse_returns_none_for_unknown_format(self) -> None:
        assert PublishedAt.parse("April 1, 2026") is None


class TestPublishedAtInvariant:
    def test_rejects_naive_datetime(self) -> None:
        with pytest.raises(ValueError, match="timezone-aware"):
            PublishedAt(datetime(2026, 4, 1, 12, 0, 0))
