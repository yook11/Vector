"""HTML 抽出層のテスト。"""

import socket
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app.collection.errors import PermanentFetchError, TemporaryFetchError
from app.collection.extraction.domain.value_objects import PublishedAt
from app.collection.extraction.extractor import (
    ArticleHtmlExtractor,
    ExtractedContent,
    ExtractionEmpty,
    _decode_html_response,
)
from app.shared.value_objects.safe_url import SafeUrl


@pytest.fixture(autouse=True)
def _stub_dns_resolver():
    """全テストで実 DNS を叩かないように ``_resolve_host`` を public IP 固定にする。

    SSRF/DNS 関連のシナリオを検証したいテストは、本 fixture の上に
    個別 patch を重ねて override する。
    """
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
            result = await extractor.fetch(SafeUrl("https://example.com/article"))

        assert isinstance(result, ExtractedContent)
        assert len(result.body) > 50
        assert result.title

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
            await extractor.fetch(SafeUrl("https://example.com/paywall"))

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
            await extractor.fetch(SafeUrl("https://example.com/error"))

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
            await extractor.fetch(SafeUrl("https://example.com/slow"))

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
            result = await extractor.fetch(SafeUrl("https://example.com/doc.pdf"))

        assert isinstance(result, ExtractionEmpty)
        assert result.reason == "not_html"

    @pytest.mark.asyncio
    async def test_returns_empty_for_minimal_content(self) -> None:
        """品質ゲートにより短すぎるコンテンツは ExtractionEmpty になる。"""
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
            result = await extractor.fetch(SafeUrl("https://example.com/short"))

        assert isinstance(result, ExtractionEmpty)
        assert result.reason in ("quality_gate", "parse_error")

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
            await extractor.fetch(SafeUrl("https://example.com/private/article"))

    @pytest.mark.asyncio
    async def test_raises_permanent_when_host_resolves_to_private_ip(self) -> None:
        """ホスト名の DNS 解決結果が private IP なら fetch せず PermanentFetchError。"""
        extractor = ArticleHtmlExtractor()
        with patch(
            "app.shared.security.ssrf_guard._resolve_host",
            new=AsyncMock(return_value=["172.18.0.5"]),
        ):
            with pytest.raises(PermanentFetchError, match="non-public address"):
                await extractor.fetch(SafeUrl("https://internal-trick.example.com/"))

    @pytest.mark.asyncio
    async def test_raises_permanent_when_host_resolves_to_link_local(self) -> None:
        """A レコードがクラウドメタデータ (169.254.169.254) を指しているケース。"""
        extractor = ArticleHtmlExtractor()
        with patch(
            "app.shared.security.ssrf_guard._resolve_host",
            new=AsyncMock(return_value=["169.254.169.254"]),
        ):
            with pytest.raises(PermanentFetchError, match="169.254.169.254"):
                await extractor.fetch(SafeUrl("https://metadata-attack.example.com/"))

    @pytest.mark.asyncio
    async def test_raises_temporary_on_dns_failure(self) -> None:
        """DNS 解決失敗は一時的失敗としてリトライ可能な分類にする。"""
        extractor = ArticleHtmlExtractor()
        with patch(
            "app.shared.security.ssrf_guard._resolve_host",
            new=AsyncMock(side_effect=socket.gaierror("nope")),
        ):
            with pytest.raises(TemporaryFetchError, match="DNS resolution failed"):
                await extractor.fetch(SafeUrl("https://nonexistent.invalid/"))

    @pytest.mark.asyncio
    async def test_raises_permanent_on_3xx_redirect(self) -> None:
        """3xx は follow せず明示的に拒否する (リダイレクト経由の SSRF 回避)。"""
        robots_resp = httpx.Response(
            404,
            request=httpx.Request("GET", "https://example.com/robots.txt"),
        )
        redirect_resp = httpx.Response(
            302,
            headers={"location": "http://169.254.169.254/"},
            request=httpx.Request("GET", "https://example.com/article"),
        )
        client = _mock_async_client([robots_resp, redirect_resp])

        extractor = ArticleHtmlExtractor()
        with (
            _patch_client(client),
            pytest.raises(PermanentFetchError, match="redirect not followed"),
        ):
            await extractor.fetch(SafeUrl("https://example.com/article"))

    @pytest.mark.asyncio
    async def test_raises_permanent_on_oversized_content_length_header(self) -> None:
        """Content-Length が上限超過なら本文を読まずに拒否する。"""
        robots_resp = httpx.Response(
            404,
            request=httpx.Request("GET", "https://example.com/robots.txt"),
        )
        huge_resp = httpx.Response(
            200,
            content=b"<html><body>ok</body></html>",
            headers={
                "content-type": "text/html",
                "content-length": str(20 * 1024 * 1024),
            },
            request=httpx.Request("GET", "https://example.com/huge"),
        )
        client = _mock_async_client([robots_resp, huge_resp])

        extractor = ArticleHtmlExtractor()
        with (
            _patch_client(client),
            pytest.raises(PermanentFetchError, match="response too large"),
        ):
            await extractor.fetch(SafeUrl("https://example.com/huge"))

    @pytest.mark.asyncio
    async def test_raises_permanent_on_oversized_actual_body(self) -> None:
        """Content-Length が無くても、実バイト数が上限超過なら拒否する。"""
        robots_resp = httpx.Response(
            404,
            request=httpx.Request("GET", "https://example.com/robots.txt"),
        )
        huge_body = b"x" * (11 * 1024 * 1024)  # 11 MiB
        huge_resp = httpx.Response(
            200,
            content=huge_body,
            headers={"content-type": "text/html"},
            request=httpx.Request("GET", "https://example.com/huge2"),
        )
        client = _mock_async_client([robots_resp, huge_resp])

        extractor = ArticleHtmlExtractor()
        with (
            _patch_client(client),
            pytest.raises(PermanentFetchError, match="response too large"),
        ):
            await extractor.fetch(SafeUrl("https://example.com/huge2"))

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
            await extractor.fetch(SafeUrl("https://example.com/a1"))
        with _patch_client(client_2):
            await extractor.fetch(SafeUrl("https://example.com/a2"))

        # 2 回目の fetch では robots.txt を再リクエストしないはず
        robots_calls_1 = [
            c for c in client_1.get.call_args_list if "robots.txt" in str(c)
        ]
        robots_calls_2 = [
            c for c in client_2.get.call_args_list if "robots.txt" in str(c)
        ]
        assert len(robots_calls_1) == 1
        assert len(robots_calls_2) == 0


class TestExtractedContentInvariant:
    """ExtractedContent のコンストラクタ invariant。"""

    def test_rejects_empty_title(self) -> None:
        with pytest.raises(ValueError, match="title"):
            ExtractedContent(title="", body="x" * 60, published_at=None)

    def test_rejects_title_over_limit(self) -> None:
        with pytest.raises(ValueError, match="title"):
            ExtractedContent(title="x" * 501, body="x" * 60, published_at=None)

    def test_rejects_short_body(self) -> None:
        with pytest.raises(ValueError, match="body"):
            ExtractedContent(title="t", body="x" * 10, published_at=None)

    def test_accepts_valid_fields(self) -> None:
        content = ExtractedContent(
            title="t",
            body="x" * 60,
            published_at=PublishedAt(datetime(2026, 4, 1, tzinfo=UTC)),
        )
        assert content.title == "t"
        assert content.published_at is not None


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
        html_bytes = (
            b'<html><head><meta charset="not-a-real-encoding">'
            b"</head><body>ok</body></html>"
        )
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
                SafeUrl("https://www.itmedia.co.jp/news/articles/test.html")
            )

        assert isinstance(result, ExtractedContent)
        assert "量子コンピューティング" in result.body
