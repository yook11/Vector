"""Tests for the article body fetcher."""

from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app.collection.article_body_fetcher import (
    ArticleBodyFetcher,
    PermanentFetchError,
    TemporaryFetchError,
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


def _mock_async_client(responses: list[httpx.Response | Exception]) -> AsyncMock:
    """Build an AsyncClient mock whose ``get`` returns/raises in sequence."""
    client = AsyncMock(spec=httpx.AsyncClient)
    client.get = AsyncMock(side_effect=responses)
    return client


def _patch_client(client: AsyncMock):
    """Patch ``httpx.AsyncClient`` so fetch() uses the given mock client."""
    return patch(
        "app.collection.article_body_fetcher.httpx.AsyncClient",
        return_value=_as_async_cm(client),
    )


def _as_async_cm(value: object) -> AsyncMock:
    """Wrap a value in an async context manager mock."""
    cm = AsyncMock()
    cm.__aenter__ = AsyncMock(return_value=value)
    cm.__aexit__ = AsyncMock(return_value=None)
    return cm


class TestArticleBodyFetcher:
    @pytest.mark.asyncio
    async def test_fetches_body_from_url(self) -> None:
        robots_resp = httpx.Response(
            404,
            request=httpx.Request("GET", "https://example.com/robots.txt"),
        )
        html_resp = httpx.Response(
            200,
            text=SAMPLE_HTML,
            headers={"content-type": "text/html; charset=utf-8"},
            request=httpx.Request("GET", "https://example.com/article"),
        )
        client = _mock_async_client([robots_resp, html_resp])

        fetcher = ArticleBodyFetcher()
        with _patch_client(client):
            result = await fetcher.fetch("https://example.com/article")

        assert result is not None
        assert len(result) > 50

    @pytest.mark.asyncio
    async def test_raises_permanent_on_403(self) -> None:
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
        client = _mock_async_client([robots_resp, error_resp])

        fetcher = ArticleBodyFetcher()
        with _patch_client(client), pytest.raises(PermanentFetchError, match="403"):
            await fetcher.fetch("https://example.com/paywall")

    @pytest.mark.asyncio
    async def test_raises_temporary_on_500(self) -> None:
        robots_resp = httpx.Response(
            404,
            request=httpx.Request("GET", "https://example.com/robots.txt"),
        )
        error_resp = httpx.Response(
            500,
            request=httpx.Request("GET", "https://example.com/error"),
        )
        error_resp.raise_for_status = lambda: (_ for _ in ()).throw(
            httpx.HTTPStatusError(
                "500", request=error_resp.request, response=error_resp
            )
        )
        client = _mock_async_client([robots_resp, error_resp])

        fetcher = ArticleBodyFetcher()
        with _patch_client(client), pytest.raises(TemporaryFetchError, match="500"):
            await fetcher.fetch("https://example.com/error")

    @pytest.mark.asyncio
    async def test_raises_temporary_on_request_error(self) -> None:
        robots_resp = httpx.Response(
            404,
            request=httpx.Request("GET", "https://example.com/robots.txt"),
        )
        client = _mock_async_client([robots_resp, httpx.ConnectTimeout("timed out")])

        fetcher = ArticleBodyFetcher()
        with (
            _patch_client(client),
            pytest.raises(TemporaryFetchError, match="timed out"),
        ):
            await fetcher.fetch("https://example.com/slow")

    @pytest.mark.asyncio
    async def test_returns_none_for_non_html(self) -> None:
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
        client = _mock_async_client([robots_resp, pdf_resp])

        fetcher = ArticleBodyFetcher()
        with _patch_client(client):
            result = await fetcher.fetch("https://example.com/doc.pdf")

        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_for_minimal_content(self) -> None:
        """Quality gate rejects content that's too short after parsing."""
        robots_resp = httpx.Response(
            404,
            request=httpx.Request("GET", "https://example.com/robots.txt"),
        )
        minimal_html = "<html><body><p>Short</p></body></html>"
        html_resp = httpx.Response(
            200,
            text=minimal_html,
            headers={"content-type": "text/html"},
            request=httpx.Request("GET", "https://example.com/short"),
        )
        client = _mock_async_client([robots_resp, html_resp])

        fetcher = ArticleBodyFetcher()
        with _patch_client(client):
            result = await fetcher.fetch("https://example.com/short")

        assert result is None

    @pytest.mark.asyncio
    async def test_raises_permanent_on_robots_blocked(self) -> None:
        robots_content = "User-agent: *\nDisallow: /private/"
        robots_resp = httpx.Response(
            200,
            text=robots_content,
            request=httpx.Request("GET", "https://example.com/robots.txt"),
        )
        client = _mock_async_client([robots_resp])

        fetcher = ArticleBodyFetcher()
        with _patch_client(client), pytest.raises(PermanentFetchError, match="robots"):
            await fetcher.fetch("https://example.com/private/article")

    @pytest.mark.asyncio
    async def test_caches_robots_txt_across_calls(self) -> None:
        """Same domain's robots.txt should be fetched only once across fetch() calls."""
        robots_resp = httpx.Response(
            404,
            request=httpx.Request("GET", "https://example.com/robots.txt"),
        )
        html_resp_1 = httpx.Response(
            200,
            text=SAMPLE_HTML,
            headers={"content-type": "text/html"},
            request=httpx.Request("GET", "https://example.com/a1"),
        )
        html_resp_2 = httpx.Response(
            200,
            text=SAMPLE_HTML,
            headers={"content-type": "text/html"},
            request=httpx.Request("GET", "https://example.com/a2"),
        )
        # Two fetches reuse the same fetcher instance (shared robots cache)
        client_1 = _mock_async_client([robots_resp, html_resp_1])
        client_2 = _mock_async_client([html_resp_2])

        fetcher = ArticleBodyFetcher()
        with _patch_client(client_1):
            await fetcher.fetch("https://example.com/a1")
        with _patch_client(client_2):
            await fetcher.fetch("https://example.com/a2")

        # Second fetch should not re-request robots.txt
        robots_calls_1 = [
            c for c in client_1.get.call_args_list if "robots.txt" in str(c)
        ]
        robots_calls_2 = [
            c for c in client_2.get.call_args_list if "robots.txt" in str(c)
        ]
        assert len(robots_calls_1) == 1
        assert len(robots_calls_2) == 0
