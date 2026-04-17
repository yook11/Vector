"""FierceBiotechFetcher の convert_entry テスト。"""

import time

from app.collection.ingestion.fetchers.rss.fierce_biotech import (
    FierceBiotechFetcher,
    _parse_fierce_date,
)


class TestParseFierceDate:
    def test_parses_lowercase_pm(self) -> None:
        result = _parse_fierce_date("Apr 17, 2026 12:23pm")
        assert result is not None
        assert result.year == 2026
        assert result.month == 4
        assert result.day == 17
        assert result.hour == 12
        assert result.minute == 23

    def test_parses_uppercase_pm(self) -> None:
        result = _parse_fierce_date("Apr 17, 2026 12:23PM")
        assert result is not None
        assert result.hour == 12

    def test_parses_am(self) -> None:
        result = _parse_fierce_date("Jan 01, 2025 09:00am")
        assert result is not None
        assert result.hour == 9

    def test_returns_none_for_invalid_format(self) -> None:
        assert _parse_fierce_date("2025-01-01T00:00:00Z") is None

    def test_returns_none_for_empty_string(self) -> None:
        assert _parse_fierce_date("") is None


class TestFierceBiotechConvertEntry:
    def test_uses_feedparser_date_when_available(self) -> None:
        """feedparser が published_parsed を返す場合はそれを優先する。"""
        entry = {
            "link": "https://fiercebiotech.com/article-1",
            "title": "Biotech News",
            "summary": "Summary",
            "published_parsed": time.struct_time((2025, 6, 15, 10, 30, 0, 6, 166, 0)),
            "published": "Jun 15, 2025 10:30am",
        }
        fetcher = FierceBiotechFetcher()
        candidate = fetcher.convert_entry(entry)

        assert candidate is not None
        assert candidate.published_at is not None
        assert candidate.published_at.month == 6

    def test_falls_back_to_custom_date_parser(self) -> None:
        """feedparser が published_parsed を返さない場合は独自パーサー。"""
        entry = {
            "link": "https://fiercebiotech.com/article-2",
            "title": "Biotech News 2",
            "summary": "Summary 2",
            "published": "Apr 17, 2026 12:23pm",
        }
        fetcher = FierceBiotechFetcher()
        candidate = fetcher.convert_entry(entry)

        assert candidate is not None
        assert candidate.published_at is not None
        assert candidate.published_at.year == 2026
        assert candidate.published_at.month == 4

    def test_returns_none_for_empty_url(self) -> None:
        entry = {"link": "", "title": "No URL", "summary": "No URL"}
        fetcher = FierceBiotechFetcher()
        assert fetcher.convert_entry(entry) is None
