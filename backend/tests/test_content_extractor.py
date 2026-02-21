"""Tests for the content extractor service."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.news import NewsArticle
from app.services.content_extractor import (
    ContentExtractionResult,
    _parse_article_html,
    extract_content,
    extract_contents,
)

SAMPLE_HTML = """
<html>
<head><title>Test Article</title></head>
<body>
<article>
<h1>Quantum Computing Breakthrough</h1>
<p>Researchers have achieved a significant milestone in quantum computing.
The team demonstrated error-corrected logical qubits operating at
unprecedented fidelity levels, marking a crucial step toward practical
quantum computers. This breakthrough could accelerate the development
of quantum applications in drug discovery, materials science, and
cryptography. Industry experts predict this will attract substantial
investment from major technology companies.</p>
</article>
</body>
</html>
"""

MINIMAL_HTML = """
<html><body><p>Short</p></body></html>
"""


class TestParseArticleHtml:
    """Tests for _parse_article_html (sync parser)."""

    def test_extracts_text_from_html(self) -> None:
        result = _parse_article_html(SAMPLE_HTML, "https://example.com/article")
        assert result is not None
        assert "quantum" in result.lower()

    def test_returns_none_for_minimal_content(self) -> None:
        result = _parse_article_html(MINIMAL_HTML, "https://example.com/short")
        assert result is None

    def test_returns_none_for_empty_html(self) -> None:
        result = _parse_article_html("<html><body></body></html>", "https://example.com")
        assert result is None


class TestExtractContent:
    """Tests for extract_content (async, uses httpx)."""

    @pytest.mark.asyncio
    async def test_extracts_content_from_url(self) -> None:
        mock_response = httpx.Response(
            200,
            text=SAMPLE_HTML,
            headers={"content-type": "text/html; charset=utf-8"},
            request=httpx.Request("GET", "https://example.com/article"),
        )
        client = AsyncMock(spec=httpx.AsyncClient)
        # robots.txt returns 404 (no robots.txt → assume allowed)
        robots_resp = httpx.Response(
            404,
            request=httpx.Request("GET", "https://example.com/robots.txt"),
        )
        client.get = AsyncMock(side_effect=[robots_resp, mock_response])

        result = await extract_content(client, "https://example.com/article", {})
        assert result is not None
        assert len(result) > 50

    @pytest.mark.asyncio
    async def test_returns_none_on_http_error(self) -> None:
        client = AsyncMock(spec=httpx.AsyncClient)
        robots_resp = httpx.Response(
            404,
            request=httpx.Request("GET", "https://example.com/robots.txt"),
        )
        error_resp = httpx.Response(
            403,
            request=httpx.Request("GET", "https://example.com/paywall"),
        )
        error_resp.raise_for_status = lambda: (_ for _ in ()).throw(
            httpx.HTTPStatusError("403", request=error_resp.request, response=error_resp)
        )
        client.get = AsyncMock(side_effect=[robots_resp, error_resp])

        result = await extract_content(client, "https://example.com/paywall", {})
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_for_non_html(self) -> None:
        client = AsyncMock(spec=httpx.AsyncClient)
        robots_resp = httpx.Response(
            404,
            request=httpx.Request("GET", "https://example.com/robots.txt"),
        )
        pdf_resp = httpx.Response(
            200,
            content=b"%PDF-1.4",
            headers={"content-type": "application/pdf"},
            request=httpx.Request("GET", "https://example.com/doc.pdf"),
        )
        client.get = AsyncMock(side_effect=[robots_resp, pdf_resp])

        result = await extract_content(client, "https://example.com/doc.pdf", {})
        assert result is None

    @pytest.mark.asyncio
    async def test_respects_robots_txt(self) -> None:
        client = AsyncMock(spec=httpx.AsyncClient)
        robots_content = "User-agent: *\nDisallow: /private/"
        robots_resp = httpx.Response(
            200,
            text=robots_content,
            request=httpx.Request("GET", "https://example.com/robots.txt"),
        )
        client.get = AsyncMock(return_value=robots_resp)

        result = await extract_content(
            client, "https://example.com/private/article", {}
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_caches_robots_txt(self) -> None:
        cache: dict = {}
        client = AsyncMock(spec=httpx.AsyncClient)
        robots_resp = httpx.Response(
            404,
            request=httpx.Request("GET", "https://example.com/robots.txt"),
        )
        html_resp = httpx.Response(
            200,
            text=SAMPLE_HTML,
            headers={"content-type": "text/html"},
            request=httpx.Request("GET", "https://example.com/a1"),
        )
        client.get = AsyncMock(side_effect=[robots_resp, html_resp, html_resp])

        await extract_content(client, "https://example.com/a1", cache)
        assert "https://example.com/robots.txt" in cache

        # Second call should use cache (no additional robots.txt request)
        await extract_content(client, "https://example.com/a2", cache)
        # robots.txt was fetched only once (first call)
        robots_calls = [
            c for c in client.get.call_args_list
            if "robots.txt" in str(c)
        ]
        assert len(robots_calls) == 1


class TestExtractContents:
    """Tests for extract_contents (batch extraction with DB persistence)."""

    @pytest.mark.asyncio
    async def test_extracts_and_persists(self, db_session: AsyncSession) -> None:
        article = NewsArticle(
            title_original="Test Article",
            url="https://example.com/test",
            source="Test",
            fetched_at=datetime.now(UTC),
        )
        db_session.add(article)
        await db_session.commit()
        await db_session.refresh(article)

        with patch(
            "app.services.content_extractor.extract_content",
            return_value="Extracted article content that is longer than fifty characters for the test to pass.",
        ):
            result = await extract_contents(db_session, [article])

        assert isinstance(result, ContentExtractionResult)
        assert result.extracted_count == 1
        assert result.skipped_count == 0
        assert result.error_count == 0

        await db_session.refresh(article)
        assert article.content is not None
        assert article.content_fetched_at is not None

    @pytest.mark.asyncio
    async def test_handles_extraction_failure(self, db_session: AsyncSession) -> None:
        article = NewsArticle(
            title_original="Fail Article",
            url="https://example.com/fail",
            source="Test",
            fetched_at=datetime.now(UTC),
        )
        db_session.add(article)
        await db_session.commit()
        await db_session.refresh(article)

        with patch(
            "app.services.content_extractor.extract_content",
            return_value=None,
        ):
            result = await extract_contents(db_session, [article])

        assert result.extracted_count == 0
        assert result.skipped_count == 1
        # content_fetched_at should be set even if content is None
        await db_session.refresh(article)
        assert article.content is None
        assert article.content_fetched_at is not None

    @pytest.mark.asyncio
    async def test_empty_articles_list(self, db_session: AsyncSession) -> None:
        result = await extract_contents(db_session, [])
        assert result.extracted_count == 0
        assert result.skipped_count == 0
        assert result.error_count == 0
