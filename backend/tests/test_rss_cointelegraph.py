"""CointelegraphFetcher の convert_entry テスト。"""

import time

from app.collection.ingestion.fetchers.rss.cointelegraph import (
    CointelegraphFetcher,
    _strip_utm_params,
)


class TestStripUtmParams:
    def test_removes_utm_params(self) -> None:
        url = "https://example.com/article?utm_source=rss&utm_medium=feed&id=123"
        assert _strip_utm_params(url) == "https://example.com/article?id=123"

    def test_removes_all_utm_params(self) -> None:
        url = "https://example.com/article?utm_source=rss&utm_campaign=test"
        assert _strip_utm_params(url) == "https://example.com/article"

    def test_preserves_non_utm_params(self) -> None:
        url = "https://example.com/article?id=123&ref=home"
        assert _strip_utm_params(url) == "https://example.com/article?id=123&ref=home"

    def test_handles_url_without_params(self) -> None:
        url = "https://example.com/article"
        assert _strip_utm_params(url) == "https://example.com/article"


class TestCointelegraphConvertEntry:
    def test_strips_utm_from_link(self) -> None:
        entry = {
            "link": "https://cointelegraph.com/news/test?utm_source=rss&utm_medium=feed",
            "title": "Test Article",
            "summary": "Test summary",
            "id": "https://cointelegraph.com/news/test?utm_source=rss",
        }
        fetcher = CointelegraphFetcher()
        candidate = fetcher.convert_entry(entry)

        assert candidate is not None
        assert str(candidate.url) == "https://cointelegraph.com/news/test"

    def test_returns_none_for_empty_url(self) -> None:
        entry = {"link": "", "title": "No URL", "summary": "No URL"}
        fetcher = CointelegraphFetcher()
        assert fetcher.convert_entry(entry) is None

    def test_preserves_title_and_description(self) -> None:
        entry = {
            "link": "https://cointelegraph.com/news/test",
            "title": "Crypto News",
            "summary": "Crypto summary",
            "published_parsed": time.struct_time((2025, 4, 17, 10, 0, 0, 3, 107, 0)),
        }
        fetcher = CointelegraphFetcher()
        candidate = fetcher.convert_entry(entry)

        assert candidate is not None
        assert candidate.title == "Crypto News"
        assert candidate.description == "Crypto summary"
        assert candidate.published_at is not None
