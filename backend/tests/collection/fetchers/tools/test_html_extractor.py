"""``HtmlContentExtractor`` のユニットテスト。"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app.collection.external_fetch_errors import (
    FetchAccessDeniedError,
    FetchOriginServerError,
    FetchRedirectBlockedError,
    FetchRobotsDisallowedError,
    FetchTimeoutError,
)
from app.collection.fetchers.tools.html_extractor import (
    ExtractedContent,
    ExtractionEmptyError,
    HtmlContentExtractor,
)
from app.shared.value_objects.safe_url import SafeUrl


@pytest.fixture(autouse=True)
def _stub_dns_resolver():
    """全テストで実 DNS を叩かないように ``_resolve_host`` を public IP 固定にする。"""
    with patch(
        "app.shared.security.ssrf_guard._resolve_host",
        new=AsyncMock(return_value=["8.8.8.8"]),
    ):
        yield


SAMPLE_HTML = """
<html>
<head>
<title>Test Article</title>
<meta property="article:published_time" content="2026-03-15T10:30:00Z" />
</head>
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
    client = AsyncMock(spec=httpx.AsyncClient)
    client.get = AsyncMock(side_effect=responses)
    return client


def _as_async_cm(value: object) -> AsyncMock:
    cm = AsyncMock()
    cm.__aenter__ = AsyncMock(return_value=value)
    cm.__aexit__ = AsyncMock(return_value=None)
    return cm


def _patch_client(client: AsyncMock):
    return patch(
        "app.collection.fetchers.tools.html_extractor.make_safe_async_client",
        return_value=_as_async_cm(client),
    )


def _httpx_error_response(status_code: int, url: str) -> httpx.Response:
    """``raise_for_status`` で HTTPStatusError を投げるレスポンス。"""
    resp = httpx.Response(status_code, request=httpx.Request("GET", url))
    resp.raise_for_status = lambda: (_ for _ in ()).throw(  # type: ignore[method-assign]
        httpx.HTTPStatusError(str(status_code), request=resp.request, response=resp)
    )
    return resp


class TestHtmlContentExtractor:
    async def test_returns_extracted_content_for_html(self) -> None:
        robots_resp = httpx.Response(
            404, request=httpx.Request("GET", "https://example.com/robots.txt")
        )
        html_resp = httpx.Response(
            200,
            text=SAMPLE_HTML,
            headers={"content-type": "text/html; charset=utf-8"},
            request=httpx.Request("GET", "https://example.com/article"),
        )
        client = _mock_async_client([robots_resp, html_resp])

        extractor = HtmlContentExtractor()
        with _patch_client(client):
            result = await extractor.fetch_and_extract(
                SafeUrl("https://example.com/article")
            )

        assert isinstance(result, ExtractedContent)
        assert len(result.body) > 50
        assert result.title

    async def test_raises_access_denied_on_403(self) -> None:
        robots_resp = httpx.Response(
            404, request=httpx.Request("GET", "https://example.com/robots.txt")
        )
        error_resp = _httpx_error_response(403, "https://example.com/paywall")
        client = _mock_async_client([robots_resp, error_resp])

        extractor = HtmlContentExtractor()
        with (
            _patch_client(client),
            pytest.raises(FetchAccessDeniedError, match="403"),
        ):
            await extractor.fetch_and_extract(SafeUrl("https://example.com/paywall"))

    async def test_raises_origin_server_error_on_500(self) -> None:
        robots_resp = httpx.Response(
            404, request=httpx.Request("GET", "https://example.com/robots.txt")
        )
        error_resp = _httpx_error_response(500, "https://example.com/error")
        client = _mock_async_client([robots_resp, error_resp])

        extractor = HtmlContentExtractor()
        with (
            _patch_client(client),
            pytest.raises(FetchOriginServerError, match="500"),
        ):
            await extractor.fetch_and_extract(SafeUrl("https://example.com/error"))

    async def test_raises_timeout_on_connect_timeout(self) -> None:
        robots_resp = httpx.Response(
            404, request=httpx.Request("GET", "https://example.com/robots.txt")
        )
        client = _mock_async_client([robots_resp, httpx.ConnectTimeout("timed out")])

        extractor = HtmlContentExtractor()
        with (
            _patch_client(client),
            pytest.raises(FetchTimeoutError, match="timed out"),
        ):
            await extractor.fetch_and_extract(SafeUrl("https://example.com/slow"))

    async def test_raises_extraction_empty_for_non_html(self) -> None:
        robots_resp = httpx.Response(
            404, request=httpx.Request("GET", "https://example.com/robots.txt")
        )
        pdf_resp = httpx.Response(
            200,
            content=b"%PDF-1.4",
            headers={"content-type": "application/pdf"},
            request=httpx.Request("GET", "https://example.com/doc.pdf"),
        )
        client = _mock_async_client([robots_resp, pdf_resp])

        extractor = HtmlContentExtractor()
        with _patch_client(client):
            with pytest.raises(ExtractionEmptyError) as exc:
                await extractor.fetch_and_extract(
                    SafeUrl("https://example.com/doc.pdf")
                )
        assert exc.value.kind == "not_html"

    async def test_raises_extraction_empty_for_minimal_content(self) -> None:
        robots_resp = httpx.Response(
            404, request=httpx.Request("GET", "https://example.com/robots.txt")
        )
        minimal_html = "<html><body><p>Short</p></body></html>"
        html_resp = httpx.Response(
            200,
            text=minimal_html,
            headers={"content-type": "text/html"},
            request=httpx.Request("GET", "https://example.com/short"),
        )
        client = _mock_async_client([robots_resp, html_resp])

        extractor = HtmlContentExtractor()
        with _patch_client(client):
            with pytest.raises(ExtractionEmptyError) as exc:
                await extractor.fetch_and_extract(SafeUrl("https://example.com/short"))
        assert exc.value.kind in ("quality_gate", "parse_error")

    async def test_raises_robots_disallowed_on_robots_blocked(self) -> None:
        robots_resp = httpx.Response(
            200,
            text="User-agent: *\nDisallow: /private/",
            request=httpx.Request("GET", "https://example.com/robots.txt"),
        )
        client = _mock_async_client([robots_resp])

        extractor = HtmlContentExtractor()
        with (
            _patch_client(client),
            pytest.raises(FetchRobotsDisallowedError, match="robots"),
        ):
            await extractor.fetch_and_extract(
                SafeUrl("https://example.com/private/article")
            )

    async def test_raises_redirect_blocked_on_3xx_redirect(self) -> None:
        robots_resp = httpx.Response(
            404, request=httpx.Request("GET", "https://example.com/robots.txt")
        )
        redirect_resp = httpx.Response(
            302,
            headers={"location": "http://169.254.169.254/"},
            request=httpx.Request("GET", "https://example.com/article"),
        )
        client = _mock_async_client([robots_resp, redirect_resp])

        extractor = HtmlContentExtractor()
        with (
            _patch_client(client),
            pytest.raises(FetchRedirectBlockedError, match="redirect"),
        ):
            await extractor.fetch_and_extract(SafeUrl("https://example.com/article"))
