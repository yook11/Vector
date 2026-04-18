"""HTML 抽出層のテスト。"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app.collection.extraction.extractor import (
    ArticleHtmlExtractor,
    HtmlExtractionResult,
    PermanentFetchError,
    TemporaryFetchError,
    _decode_html_response,
    _parse_extracted_date,
)

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

SAMPLE_HTML_NO_DATE = """
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
    """``get`` が順に返す/raise する AsyncClient モックを構築する。"""
    client = AsyncMock(spec=httpx.AsyncClient)
    client.get = AsyncMock(side_effect=responses)
    return client


def _patch_client(client: AsyncMock):
    """``httpx.AsyncClient`` を patch して fetch() が指定モックを使うようにする。"""
    return patch(
        "app.collection.extraction.extractor.httpx.AsyncClient",
        return_value=_as_async_cm(client),
    )


def _as_async_cm(value: object) -> AsyncMock:
    """値を async context manager モックでラップする。"""
    cm = AsyncMock()
    cm.__aenter__ = AsyncMock(return_value=value)
    cm.__aexit__ = AsyncMock(return_value=None)
    return cm


class TestParseExtractedDate:
    def test_parses_iso_datetime(self) -> None:
        result = _parse_extracted_date("2026-03-15T10:30:00")
        assert result == datetime(2026, 3, 15, 10, 30, 0, tzinfo=UTC)

    def test_parses_date_only(self) -> None:
        result = _parse_extracted_date("2026-03-15")
        assert result == datetime(2026, 3, 15, 0, 0, 0, tzinfo=UTC)

    def test_returns_none_for_none(self) -> None:
        assert _parse_extracted_date(None) is None

    def test_returns_none_for_invalid(self) -> None:
        assert _parse_extracted_date("not-a-date") is None


class TestArticleHtmlExtractor:
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

        extractor = ArticleHtmlExtractor()
        with _patch_client(client):
            result = await extractor.fetch("https://example.com/article")

        assert isinstance(result, HtmlExtractionResult)
        assert result.body is not None
        assert len(result.body) > 50

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

        extractor = ArticleHtmlExtractor()
        with _patch_client(client), pytest.raises(PermanentFetchError, match="403"):
            await extractor.fetch("https://example.com/paywall")

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

        extractor = ArticleHtmlExtractor()
        with _patch_client(client), pytest.raises(TemporaryFetchError, match="500"):
            await extractor.fetch("https://example.com/error")

    @pytest.mark.asyncio
    async def test_raises_temporary_on_request_error(self) -> None:
        robots_resp = httpx.Response(
            404,
            request=httpx.Request("GET", "https://example.com/robots.txt"),
        )
        client = _mock_async_client([robots_resp, httpx.ConnectTimeout("timed out")])

        extractor = ArticleHtmlExtractor()
        with (
            _patch_client(client),
            pytest.raises(TemporaryFetchError, match="timed out"),
        ):
            await extractor.fetch("https://example.com/slow")

    @pytest.mark.asyncio
    async def test_returns_empty_result_for_non_html(self) -> None:
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

        extractor = ArticleHtmlExtractor()
        with _patch_client(client):
            result = await extractor.fetch("https://example.com/doc.pdf")

        assert result.body is None
        assert result.published_at is None

    @pytest.mark.asyncio
    async def test_returns_none_body_for_minimal_content(self) -> None:
        """品質ゲートによりパース後に短すぎるコンテンツは body=None になる。"""
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

        extractor = ArticleHtmlExtractor()
        with _patch_client(client):
            result = await extractor.fetch("https://example.com/short")

        assert result.body is None

    @pytest.mark.asyncio
    async def test_raises_permanent_on_robots_blocked(self) -> None:
        robots_content = "User-agent: *\nDisallow: /private/"
        robots_resp = httpx.Response(
            200,
            text=robots_content,
            request=httpx.Request("GET", "https://example.com/robots.txt"),
        )
        client = _mock_async_client([robots_resp])

        extractor = ArticleHtmlExtractor()
        with _patch_client(client), pytest.raises(PermanentFetchError, match="robots"):
            await extractor.fetch("https://example.com/private/article")

    @pytest.mark.asyncio
    async def test_caches_robots_txt_across_calls(self) -> None:
        """同一ドメインの robots.txt は fetch() 呼び出し間で 1 回だけ取得される。"""
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
        # 2 回の fetch は同じ extractor インスタンスを再利用 (robots cache 共有)
        client_1 = _mock_async_client([robots_resp, html_resp_1])
        client_2 = _mock_async_client([html_resp_2])

        extractor = ArticleHtmlExtractor()
        with _patch_client(client_1):
            await extractor.fetch("https://example.com/a1")
        with _patch_client(client_2):
            await extractor.fetch("https://example.com/a2")

        # 2 回目の fetch では robots.txt を再リクエストしないはず
        robots_calls_1 = [
            c for c in client_1.get.call_args_list if "robots.txt" in str(c)
        ]
        robots_calls_2 = [
            c for c in client_2.get.call_args_list if "robots.txt" in str(c)
        ]
        assert len(robots_calls_1) == 1
        assert len(robots_calls_2) == 0


class TestDecodeHtmlResponse:
    """_decode_html_response のエンコーディング検出テスト。"""

    def test_uses_response_text_when_charset_in_content_type(self) -> None:
        """Content-Type に charset があれば httpx のデコード結果をそのまま使う。"""
        resp = httpx.Response(
            200,
            text="<html><body>テスト</body></html>",
            headers={"content-type": "text/html; charset=utf-8"},
            request=httpx.Request("GET", "https://example.com/article"),
        )
        assert _decode_html_response(resp) == "<html><body>テスト</body></html>"

    def test_decodes_shift_jis_from_meta_charset(self) -> None:
        """Content-Type に charset がなく meta charset="Shift_JIS" の場合、
        バイト列から Shift_JIS でデコードする。"""
        html_text = (
            '<html><head><meta charset="Shift_JIS"></head>'
            "<body><p>日本語テスト記事</p></body></html>"
        )
        sjis_bytes = html_text.encode("shift_jis")

        resp = httpx.Response(
            200,
            content=sjis_bytes,
            headers={"content-type": "text/html"},
            request=httpx.Request("GET", "https://www.itmedia.co.jp/article"),
        )
        decoded = _decode_html_response(resp)
        assert "日本語テスト記事" in decoded

    def test_decodes_from_http_equiv_charset(self) -> None:
        """meta http-equiv の charset 指定からもデコードできる。"""
        html_text = (
            "<html><head>"
            '<meta http-equiv="Content-Type" content="text/html; charset=Shift_JIS">'
            "</head><body><p>テスト本文</p></body></html>"
        )
        sjis_bytes = html_text.encode("shift_jis")

        resp = httpx.Response(
            200,
            content=sjis_bytes,
            headers={"content-type": "text/html"},
            request=httpx.Request("GET", "https://www.itmedia.co.jp/article"),
        )
        decoded = _decode_html_response(resp)
        assert "テスト本文" in decoded

    def test_falls_back_to_response_text_when_no_charset(self) -> None:
        """meta charset もなければ httpx デフォルト（UTF-8）にフォールバックする。"""
        resp = httpx.Response(
            200,
            text="<html><body>plain text</body></html>",
            headers={"content-type": "text/html"},
            request=httpx.Request("GET", "https://example.com/article"),
        )
        assert "plain text" in _decode_html_response(resp)

    def test_falls_back_on_invalid_charset(self) -> None:
        """meta charset が不正なエンコーディング名でもクラッシュしない。"""
        html_bytes = b'<html><head><meta charset="not-a-real-encoding"></head><body>ok</body></html>'
        resp = httpx.Response(
            200,
            content=html_bytes,
            headers={"content-type": "text/html"},
            request=httpx.Request("GET", "https://example.com/article"),
        )
        result = _decode_html_response(resp)
        assert isinstance(result, str)

    @pytest.mark.asyncio
    async def test_extractor_handles_shift_jis_html(self) -> None:
        """ArticleHtmlExtractor が Shift_JIS の HTML を文字化けなく抽出する。"""
        html_text = (
            '<html><head><meta charset="Shift_JIS"></head>'
            "<body><article>"
            "<h1>量子コンピューティングの進展</h1>"
            "<p>研究者たちは量子コンピューティングにおける重要なマイルストーンを達成した。"
            "チームはエラー訂正された論理量子ビットが前例のない忠実度で動作することを実証し、"
            "実用的な量子コンピュータへの重要な一歩を踏み出した。"
            "この進展は、創薬、材料科学、暗号技術における量子アプリケーションの開発を加速させる可能性がある。"
            "</p></article></body></html>"
        )
        sjis_bytes = html_text.encode("shift_jis")

        robots_resp = httpx.Response(
            404,
            request=httpx.Request("GET", "https://www.itmedia.co.jp/robots.txt"),
        )
        html_resp = httpx.Response(
            200,
            content=sjis_bytes,
            headers={"content-type": "text/html"},
            request=httpx.Request(
                "GET", "https://www.itmedia.co.jp/news/articles/test.html"
            ),
        )
        client = _mock_async_client([robots_resp, html_resp])

        extractor = ArticleHtmlExtractor()
        with _patch_client(client):
            result = await extractor.fetch(
                "https://www.itmedia.co.jp/news/articles/test.html"
            )

        assert result.body is not None
        assert "量子コンピューティング" in result.body
