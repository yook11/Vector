"""Tests for the content extractor service."""

import asyncio
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.news import NewsArticle
from app.services.content_extractor import (
    ContentExtractionResult,
    DomainRateLimiter,
    RobotsCache,
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
        result = _parse_article_html(
            "<html><body></body></html>", "https://example.com"
        )
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

        robots = RobotsCache()
        result = await extract_content(client, "https://example.com/article", robots)
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
            httpx.HTTPStatusError(
                "403", request=error_resp.request, response=error_resp
            )
        )
        client.get = AsyncMock(side_effect=[robots_resp, error_resp])

        robots = RobotsCache()
        result = await extract_content(client, "https://example.com/paywall", robots)
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

        robots = RobotsCache()
        result = await extract_content(client, "https://example.com/doc.pdf", robots)
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
            client, "https://example.com/private/article", RobotsCache()
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_caches_robots_txt(self) -> None:
        cache = RobotsCache()
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
        assert "example.com" in cache._cache

        # Second call should use cache (no additional robots.txt request)
        await extract_content(client, "https://example.com/a2", cache)
        # robots.txt was fetched only once (first call)
        robots_calls = [c for c in client.get.call_args_list if "robots.txt" in str(c)]
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

        with (
            patch(
                "app.services.content_extractor.extract_content",
                return_value=(
                    "Extracted article content that is longer"
                    " than fifty characters for the test."
                ),
            ),
            patch.object(settings, "content_domain_delay", 0.0),
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

        with (
            patch(
                "app.services.content_extractor.extract_content",
                return_value=None,
            ),
            patch.object(settings, "content_domain_delay", 0.0),
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


class TestDomainRateLimiter:
    """Tests for DomainRateLimiter concurrency control."""

    @pytest.mark.asyncio
    async def test_different_domains_parallel(self) -> None:
        """Requests to different domains should run in parallel."""
        limiter = DomainRateLimiter(max_concurrent=10, domain_delay=0.5)
        start = asyncio.get_event_loop().time()

        async def task(url: str) -> None:
            async with limiter.acquire(url):
                pass  # work is instant; only domain_delay matters

        await asyncio.gather(
            task("https://a.com/1"),
            task("https://b.com/1"),
            task("https://c.com/1"),
        )

        total = asyncio.get_event_loop().time() - start
        # 3 different domains run in parallel → total ≈ 0.5s, not 1.5s
        assert total < 1.0

    @pytest.mark.asyncio
    async def test_same_domain_serial(self) -> None:
        """Requests to the same domain should be serialized with delay."""
        limiter = DomainRateLimiter(max_concurrent=10, domain_delay=0.1)
        timestamps: list[float] = []
        start = asyncio.get_event_loop().time()

        async def task(url: str) -> None:
            async with limiter.acquire(url):
                timestamps.append(asyncio.get_event_loop().time() - start)

        await asyncio.gather(
            task("https://a.com/1"),
            task("https://a.com/2"),
            task("https://a.com/3"),
        )

        total = asyncio.get_event_loop().time() - start
        # 3 requests × 0.1s delay = at least 0.3s
        assert total >= 0.3

        # Each subsequent request should be delayed by ~0.1s
        for i in range(1, len(timestamps)):
            gap = timestamps[i] - timestamps[i - 1]
            assert gap >= 0.09  # allow small timing tolerance

    @pytest.mark.asyncio
    async def test_respects_max_concurrent(self) -> None:
        """Should not exceed max_concurrent simultaneous connections."""
        limiter = DomainRateLimiter(max_concurrent=2, domain_delay=0.0)
        concurrent_count = 0
        max_concurrent_seen = 0

        async def task(url: str) -> None:
            nonlocal concurrent_count, max_concurrent_seen
            async with limiter.acquire(url):
                concurrent_count += 1
                max_concurrent_seen = max(max_concurrent_seen, concurrent_count)
                await asyncio.sleep(0.05)  # simulate work
                concurrent_count -= 1

        await asyncio.gather(
            task("https://a.com/1"),
            task("https://b.com/1"),
            task("https://c.com/1"),
            task("https://d.com/1"),
        )

        assert max_concurrent_seen <= 2


class TestRobotsCache:
    """Tests for RobotsCache deduplication."""

    @pytest.mark.asyncio
    async def test_no_duplicate_fetch(self) -> None:
        """Same domain's robots.txt should be fetched only once."""
        cache = RobotsCache()
        client = AsyncMock(spec=httpx.AsyncClient)
        robots_resp = httpx.Response(
            404,
            request=httpx.Request("GET", "https://example.com/robots.txt"),
        )
        client.get = AsyncMock(return_value=robots_resp)

        await cache.check(client, "https://example.com/article1")
        await cache.check(client, "https://example.com/article2")

        # robots.txt should be fetched only once
        assert client.get.call_count == 1
        assert "example.com" in cache._cache


class TestExtractContentsParallel:
    """Tests for parallelized extract_contents behavior."""

    @pytest.mark.asyncio
    async def test_parallel_success(self, db_session: AsyncSession) -> None:
        """Multiple articles from different domains should all succeed."""
        articles = []
        for i, domain in enumerate(["a.com", "b.com", "c.com"]):
            article = NewsArticle(
                title_original=f"Article {i}",
                url=f"https://{domain}/article",
                source="Test",
                fetched_at=datetime.now(UTC),
            )
            db_session.add(article)
            articles.append(article)
        await db_session.commit()
        for a in articles:
            await db_session.refresh(a)

        with (
            patch(
                "app.services.content_extractor.extract_content",
                return_value=(
                    "This is extracted content that exceeds"
                    " fifty characters for the test."
                ),
            ),
            patch.object(settings, "content_domain_delay", 0.0),
        ):
            result = await extract_contents(db_session, articles)

        assert result.extracted_count == 3
        assert result.skipped_count == 0
        assert result.error_count == 0

        for a in articles:
            await db_session.refresh(a)
            assert a.content is not None
            assert a.content_fetched_at is not None

    @pytest.mark.asyncio
    async def test_partial_failure(self, db_session: AsyncSession) -> None:
        """One article's failure should not affect others."""
        articles = []
        for i, domain in enumerate(["good.com", "bad.com", "ok.com"]):
            article = NewsArticle(
                title_original=f"Article {i}",
                url=f"https://{domain}/article",
                source="Test",
                fetched_at=datetime.now(UTC),
            )
            db_session.add(article)
            articles.append(article)
        await db_session.commit()
        for a in articles:
            await db_session.refresh(a)

        call_count = 0

        async def mock_extract(
            client: httpx.AsyncClient, url: str, robots_cache: RobotsCache
        ) -> str | None:
            nonlocal call_count
            call_count += 1
            if "bad.com" in url:
                raise ConnectionError("simulated failure")
            return (
                "Extracted content that is definitely longer"
                " than fifty characters for test pass."
            )

        with (
            patch(
                "app.services.content_extractor.extract_content",
                side_effect=mock_extract,
            ),
            patch.object(settings, "content_domain_delay", 0.0),
        ):
            result = await extract_contents(db_session, articles)

        assert result.extracted_count == 2
        assert result.error_count == 1
        assert len(result.errors) == 1
        assert "bad.com" in result.errors[0] or "simulated failure" in result.errors[0]

        # Failed article should have incremented content_fetch_attempts
        bad_article = articles[1]  # bad.com
        await db_session.refresh(bad_article)
        assert bad_article.content_fetch_attempts == 1
        assert bad_article.content_fetched_at is None  # not set on error

        # Successful articles should not have incremented content_fetch_attempts
        for a in [articles[0], articles[2]]:
            await db_session.refresh(a)
            assert a.content_fetch_attempts == 0
