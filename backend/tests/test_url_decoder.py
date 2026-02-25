"""Tests for the URL decoder service."""

from unittest.mock import patch

import pytest

from app.services.url_decoder import decode_urls, is_google_news_url


class TestIsGoogleNewsUrl:
    """Tests for is_google_news_url helper."""

    def test_google_news_rss_url(self) -> None:
        url = "https://news.google.com/rss/articles/CBMiSGh0dHBz"
        assert is_google_news_url(url) is True

    def test_google_news_stories_url(self) -> None:
        url = "https://news.google.com/stories/CAAq"
        assert is_google_news_url(url) is True

    def test_regular_url(self) -> None:
        url = "https://www.reuters.com/technology/article-123"
        assert is_google_news_url(url) is False

    def test_empty_string(self) -> None:
        assert is_google_news_url("") is False

    def test_other_google_domain(self) -> None:
        url = "https://www.google.com/search?q=test"
        assert is_google_news_url(url) is False


class TestDecodeUrls:
    """Tests for decode_urls batch decoder."""

    @pytest.mark.asyncio
    async def test_passthrough_non_google_urls(self) -> None:
        urls = [
            "https://www.reuters.com/article-1",
            "https://www.bbc.com/news/article-2",
        ]
        result = await decode_urls(urls)
        assert result == {
            "https://www.reuters.com/article-1": "https://www.reuters.com/article-1",
            "https://www.bbc.com/news/article-2": "https://www.bbc.com/news/article-2",
        }

    @pytest.mark.asyncio
    async def test_empty_list(self) -> None:
        result = await decode_urls([])
        assert result == {}

    @pytest.mark.asyncio
    async def test_decodes_google_news_urls(self) -> None:
        google_url = "https://news.google.com/rss/articles/CBMiSGh0dHBz"
        real_url = "https://www.reuters.com/real-article"

        with patch(
            "app.services.url_decoder._decode_single",
            return_value={"status": True, "decoded_url": real_url},
        ):
            result = await decode_urls([google_url])

        assert result[google_url] == real_url

    @pytest.mark.asyncio
    async def test_fallback_on_decode_failure(self) -> None:
        google_url = "https://news.google.com/rss/articles/CBMiSGh0dHBz"

        with patch(
            "app.services.url_decoder._decode_single",
            return_value={"status": False, "message": "Failed to decode"},
        ):
            result = await decode_urls([google_url])

        # Falls back to original URL
        assert result[google_url] == google_url

    @pytest.mark.asyncio
    async def test_mixed_urls(self) -> None:
        google_url = "https://news.google.com/rss/articles/CBMi123"
        regular_url = "https://example.com/article"
        real_url = "https://www.reuters.com/decoded"

        with patch(
            "app.services.url_decoder._decode_single",
            return_value={"status": True, "decoded_url": real_url},
        ):
            result = await decode_urls([google_url, regular_url])

        assert result[google_url] == real_url
        assert result[regular_url] == regular_url
