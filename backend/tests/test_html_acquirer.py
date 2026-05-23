"""HTML 取得層 (acquirer) のテスト。"""

import socket
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from structlog.testing import capture_logs
from trafilatura.settings import Document as TrafilaturaDocument

from app.collection.article_completion.acquirer import (
    AcquiredContent,
    ArticleHtmlAcquirer,
    RawResponse,
    _decode_html_response,
)
from app.collection.article_completion.acquisition_failure import (
    FetchFailed,
    NotHtml,
    ParseCrashed,
    ParserGaveUp,
    QualityGateFailed,
)
from app.collection.domain.article_limits import (
    ARTICLE_BODY_MIN_LENGTH,
    ARTICLE_TITLE_MAX_LENGTH,
)
from app.collection.domain.value_objects import PublishedAt
from app.collection.external_fetch_errors import (
    FetchAccessDeniedError,
    FetchNetworkError,
    FetchOriginServerError,
    FetchRedirectBlockedError,
    FetchResponseTooLargeError,
    FetchRobotsDisallowedError,
    FetchSsrfBlockedError,
    FetchTimeoutError,
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
    """``make_safe_async_client`` を patch して acquire() がモックを使うようにする。"""
    return patch(
        "app.collection.article_completion.acquirer.make_safe_async_client",
        return_value=_as_async_cm(client),
    )


def _as_async_cm(value: object) -> AsyncMock:
    """値を async context manager モックでラップする。"""
    cm = AsyncMock()
    cm.__aenter__ = AsyncMock(return_value=value)
    cm.__aexit__ = AsyncMock(return_value=None)
    return cm


def _raw_from_httpx(resp: httpx.Response) -> RawResponse:
    """httpx.Response を _fetch が生成するのと同じ RawResponse に畳む。"""
    return RawResponse(
        url=str(resp.url),
        content_type=resp.headers.get("content-type", ""),
        charset_from_header=resp.charset_encoding,
        content=resp.content,
        decoded_text=resp.text,
    )


class TestArticleHtmlAcquirer:
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

        acquirer = ArticleHtmlAcquirer()
        with _patch_client(client):
            result = await acquirer.acquire(SafeUrl("https://example.com/article"))

        assert isinstance(result, AcquiredContent)
        assert len(result.body) > 50
        assert result.title

    @pytest.mark.asyncio
    async def test_access_denied_403_returns_fetch_failed(self) -> None:
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

        acquirer = ArticleHtmlAcquirer()
        with _patch_client(client):
            result = await acquirer.acquire(SafeUrl("https://example.com/paywall"))

        assert isinstance(result, FetchFailed)
        assert isinstance(result.error, FetchAccessDeniedError)
        assert "403" in str(result.error)

    @pytest.mark.asyncio
    async def test_access_denied_401_returns_fetch_failed(self) -> None:
        """paywall (WSJ 等) は 401 を返すため access-denied origin error にする。"""
        robots_resp = httpx.Response(
            404,
            request=httpx.Request("GET", "https://example.com/robots.txt"),
        )
        error_resp = httpx.Response(
            401,
            request=httpx.Request("GET", "https://example.com/paywall"),
        )
        error_resp.raise_for_status = lambda: (_ for _ in ()).throw(
            httpx.HTTPStatusError(
                "401", request=error_resp.request, response=error_resp
            )
        )
        client = _mock_async_client([robots_resp, error_resp])

        acquirer = ArticleHtmlAcquirer()
        with _patch_client(client):
            result = await acquirer.acquire(SafeUrl("https://example.com/paywall"))

        assert isinstance(result, FetchFailed)
        assert isinstance(result.error, FetchAccessDeniedError)
        assert "401" in str(result.error)

    @pytest.mark.asyncio
    async def test_origin_server_error_500_returns_fetch_failed(self) -> None:
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

        acquirer = ArticleHtmlAcquirer()
        with _patch_client(client):
            result = await acquirer.acquire(SafeUrl("https://example.com/error"))

        assert isinstance(result, FetchFailed)
        assert isinstance(result.error, FetchOriginServerError)
        assert "500" in str(result.error)

    @pytest.mark.asyncio
    async def test_connect_timeout_returns_fetch_failed(self) -> None:
        robots_resp = httpx.Response(
            404,
            request=httpx.Request("GET", "https://example.com/robots.txt"),
        )
        client = _mock_async_client([robots_resp, httpx.ConnectTimeout("timed out")])

        acquirer = ArticleHtmlAcquirer()
        with _patch_client(client):
            result = await acquirer.acquire(SafeUrl("https://example.com/slow"))

        assert isinstance(result, FetchFailed)
        assert isinstance(result.error, FetchTimeoutError)
        assert "timed out" in str(result.error)

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

        acquirer = ArticleHtmlAcquirer()
        with _patch_client(client):
            result = await acquirer.acquire(SafeUrl("https://example.com/doc.pdf"))

        assert isinstance(result, NotHtml)
        assert result.content_type == "application/pdf"

    @pytest.mark.asyncio
    async def test_returns_empty_for_minimal_content(self) -> None:
        """品質ゲートにより短すぎるコンテンツは failure variant になる。"""
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

        acquirer = ArticleHtmlAcquirer()
        with _patch_client(client):
            result = await acquirer.acquire(SafeUrl("https://example.com/short"))

        # trafilatura が None を返す (ParserGaveUp) または品質ゲート未達
        # (QualityGateFailed) のどちらか。decode/parse 例外は本テストでは想定しない。
        assert isinstance(result, ParserGaveUp | QualityGateFailed)
        if isinstance(result, QualityGateFailed):
            assert result.body_length < 50

    @pytest.mark.asyncio
    async def test_robots_blocked_returns_fetch_failed(self) -> None:
        robots_content = "User-agent: *\nDisallow: /private/"
        robots_resp = httpx.Response(
            200,
            text=robots_content,
            request=httpx.Request("GET", "https://example.com/robots.txt"),
        )
        client = _mock_async_client([robots_resp])

        acquirer = ArticleHtmlAcquirer()
        with _patch_client(client):
            result = await acquirer.acquire(
                SafeUrl("https://example.com/private/article")
            )

        assert isinstance(result, FetchFailed)
        assert isinstance(result.error, FetchRobotsDisallowedError)
        assert "robots" in str(result.error)

    @pytest.mark.asyncio
    async def test_ssrf_private_ip_returns_fetch_failed(self) -> None:
        """ホスト名の DNS 解決結果が private IP なら fetch せず SSRF block。"""
        acquirer = ArticleHtmlAcquirer()
        with patch(
            "app.shared.security.ssrf_guard._resolve_host",
            new=AsyncMock(return_value=["172.18.0.5"]),
        ):
            result = await acquirer.acquire(
                SafeUrl("https://internal-trick.example.com/")
            )

        assert isinstance(result, FetchFailed)
        assert isinstance(result.error, FetchSsrfBlockedError)
        assert "non-public address" in str(result.error)

    @pytest.mark.asyncio
    async def test_ssrf_link_local_returns_fetch_failed(self) -> None:
        """A レコードがクラウドメタデータ (169.254.169.254) を指しているケース。"""
        acquirer = ArticleHtmlAcquirer()
        with patch(
            "app.shared.security.ssrf_guard._resolve_host",
            new=AsyncMock(return_value=["169.254.169.254"]),
        ):
            result = await acquirer.acquire(
                SafeUrl("https://metadata-attack.example.com/")
            )

        assert isinstance(result, FetchFailed)
        assert isinstance(result.error, FetchSsrfBlockedError)
        assert "169.254.169.254" in str(result.error)

    @pytest.mark.asyncio
    async def test_dns_failure_returns_fetch_failed(self) -> None:
        """DNS 解決失敗は network origin error (disposition で retryable)。"""
        acquirer = ArticleHtmlAcquirer()
        with patch(
            "app.shared.security.ssrf_guard._resolve_host",
            new=AsyncMock(side_effect=socket.gaierror("nope")),
        ):
            result = await acquirer.acquire(SafeUrl("https://nonexistent.invalid/"))

        assert isinstance(result, FetchFailed)
        assert isinstance(result.error, FetchNetworkError)
        assert "DNS resolution failed" in str(result.error)

    @pytest.mark.asyncio
    async def test_3xx_redirect_returns_fetch_failed(self) -> None:
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

        acquirer = ArticleHtmlAcquirer()
        with _patch_client(client):
            result = await acquirer.acquire(SafeUrl("https://example.com/article"))

        assert isinstance(result, FetchFailed)
        assert isinstance(result.error, FetchRedirectBlockedError)
        assert "redirect not followed" in str(result.error)

    @pytest.mark.asyncio
    async def test_oversized_content_length_header_returns_fetch_failed(
        self,
    ) -> None:
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

        acquirer = ArticleHtmlAcquirer()
        with _patch_client(client):
            result = await acquirer.acquire(SafeUrl("https://example.com/huge"))

        assert isinstance(result, FetchFailed)
        assert isinstance(result.error, FetchResponseTooLargeError)
        assert "response too large" in str(result.error)

    @pytest.mark.asyncio
    async def test_oversized_actual_body_returns_fetch_failed(self) -> None:
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

        acquirer = ArticleHtmlAcquirer()
        with _patch_client(client):
            result = await acquirer.acquire(SafeUrl("https://example.com/huge2"))

        assert isinstance(result, FetchFailed)
        assert isinstance(result.error, FetchResponseTooLargeError)
        assert "response too large" in str(result.error)

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
        # 2 回の fetch は同じ acquirer インスタンスを再利用 (robots cache 共有)
        client_1 = _mock_async_client([robots_resp, html_resp_1])
        client_2 = _mock_async_client([html_resp_2])

        acquirer = ArticleHtmlAcquirer()
        with _patch_client(client_1):
            await acquirer.acquire(SafeUrl("https://example.com/a1"))
        with _patch_client(client_2):
            await acquirer.acquire(SafeUrl("https://example.com/a2"))

        # 2 回目の fetch では robots.txt を再リクエストしないはず
        robots_calls_1 = [
            c for c in client_1.get.call_args_list if "robots.txt" in str(c)
        ]
        robots_calls_2 = [
            c for c in client_2.get.call_args_list if "robots.txt" in str(c)
        ]
        assert len(robots_calls_1) == 1
        assert len(robots_calls_2) == 0


class TestAcquiredContentInvariant:
    """AcquiredContent のコンストラクタ invariant。"""

    def test_rejects_empty_title(self) -> None:
        with pytest.raises(ValueError, match="title"):
            AcquiredContent(title="", body="x" * 60, published_at=None)

    def test_rejects_title_over_limit(self) -> None:
        with pytest.raises(ValueError, match="title"):
            AcquiredContent(title="x" * 501, body="x" * 60, published_at=None)

    def test_rejects_short_body(self) -> None:
        with pytest.raises(ValueError, match="body"):
            AcquiredContent(title="t", body="x" * 10, published_at=None)

    def test_accepts_valid_fields(self) -> None:
        content = AcquiredContent(
            title="t",
            body="x" * 60,
            published_at=PublishedAt(datetime(2026, 4, 1, tzinfo=UTC)),
        )
        assert content.title == "t"
        assert content.published_at is not None


class TestAcquiredContentTryCreate:
    """AcquiredContent.try_create: 品質ゲート判定の所有テスト。

    閾値は ``article_limits`` SSoT を import して導出する (literal 直書きしない)。
    成功時は ``AcquiredContent``、未達時は証拠付き ``QualityGateFailed`` を値で返す
    契約を確かめる。
    """

    def test_valid_material_returns_acquired_content(self) -> None:
        body = "x" * ARTICLE_BODY_MIN_LENGTH
        outcome = AcquiredContent.try_create(
            raw_title="Title", stripped_body=body, raw_date=None
        )
        assert isinstance(outcome, AcquiredContent)

    def test_short_body_returns_quality_failure_with_body_length(self) -> None:
        body = "x" * (ARTICLE_BODY_MIN_LENGTH - 1)
        outcome = AcquiredContent.try_create(
            raw_title="Title", stripped_body=body, raw_date=None
        )
        assert isinstance(outcome, QualityGateFailed)
        assert outcome.body_length == len(body)

    def test_short_body_keeps_title_present_true(self) -> None:
        body = "x" * (ARTICLE_BODY_MIN_LENGTH - 1)
        outcome = AcquiredContent.try_create(
            raw_title="Title", stripped_body=body, raw_date=None
        )
        assert isinstance(outcome, QualityGateFailed)
        assert outcome.title_present is True

    def test_short_body_keeps_body_sample(self) -> None:
        body = "x" * (ARTICLE_BODY_MIN_LENGTH - 1)
        outcome = AcquiredContent.try_create(
            raw_title="Title", stripped_body=body, raw_date=None
        )
        assert isinstance(outcome, QualityGateFailed)
        assert outcome.body_sample == body

    def test_empty_title_returns_quality_failure(self) -> None:
        body = "x" * ARTICLE_BODY_MIN_LENGTH
        outcome = AcquiredContent.try_create(
            raw_title="", stripped_body=body, raw_date=None
        )
        assert isinstance(outcome, QualityGateFailed)
        assert outcome.title_present is False

    def test_none_title_returns_quality_failure(self) -> None:
        body = "x" * ARTICLE_BODY_MIN_LENGTH
        outcome = AcquiredContent.try_create(
            raw_title=None, stripped_body=body, raw_date=None
        )
        assert isinstance(outcome, QualityGateFailed)
        assert outcome.title_present is False

    def test_title_present_but_body_at_least_min_drops_body_sample(self) -> None:
        # body は閾値以上で title 欠落により落ちる → 冒頭断片は残さない。
        body = "x" * ARTICLE_BODY_MIN_LENGTH
        outcome = AcquiredContent.try_create(
            raw_title=None, stripped_body=body, raw_date=None
        )
        assert isinstance(outcome, QualityGateFailed)
        assert outcome.body_sample is None

    def test_empty_body_drops_body_sample(self) -> None:
        outcome = AcquiredContent.try_create(
            raw_title="Title", stripped_body="", raw_date=None
        )
        assert isinstance(outcome, QualityGateFailed)
        assert outcome.body_sample is None

    def test_html_tags_stripped_from_title(self) -> None:
        body = "x" * ARTICLE_BODY_MIN_LENGTH
        outcome = AcquiredContent.try_create(
            raw_title="<b>Bold Title</b>", stripped_body=body, raw_date=None
        )
        assert isinstance(outcome, AcquiredContent)
        assert outcome.title == "Bold Title"

    def test_title_over_limit_is_truncated(self) -> None:
        body = "x" * ARTICLE_BODY_MIN_LENGTH
        outcome = AcquiredContent.try_create(
            raw_title="t" * (ARTICLE_TITLE_MAX_LENGTH + 10),
            stripped_body=body,
            raw_date=None,
        )
        assert isinstance(outcome, AcquiredContent)
        assert len(outcome.title) == ARTICLE_TITLE_MAX_LENGTH

    def test_parseable_date_populates_published_at(self) -> None:
        body = "x" * ARTICLE_BODY_MIN_LENGTH
        outcome = AcquiredContent.try_create(
            raw_title="Title", stripped_body=body, raw_date="2026-03-15T10:30:00"
        )
        assert isinstance(outcome, AcquiredContent)
        assert outcome.published_at is not None

    def test_unparseable_date_leaves_published_at_none(self) -> None:
        body = "x" * ARTICLE_BODY_MIN_LENGTH
        outcome = AcquiredContent.try_create(
            raw_title="Title", stripped_body=body, raw_date="not-a-date"
        )
        assert isinstance(outcome, AcquiredContent)
        assert outcome.published_at is None

    def test_none_date_leaves_published_at_none(self) -> None:
        body = "x" * ARTICLE_BODY_MIN_LENGTH
        outcome = AcquiredContent.try_create(
            raw_title="Title", stripped_body=body, raw_date=None
        )
        assert isinstance(outcome, AcquiredContent)
        assert outcome.published_at is None


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
        decoded = _decode_html_response(_raw_from_httpx(resp))
        assert decoded == "<html><body>テスト</body></html>"

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
        decoded = _decode_html_response(_raw_from_httpx(resp))
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
        decoded = _decode_html_response(_raw_from_httpx(resp))
        assert "テスト本文" in decoded

    def test_falls_back_to_response_text_when_no_charset(self) -> None:
        """meta charset もなければ httpx デフォルト（UTF-8）にフォールバックする。"""
        resp = httpx.Response(
            200,
            text="<html><body>plain text</body></html>",
            headers={"content-type": "text/html"},
            request=httpx.Request("GET", "https://example.com/article"),
        )
        assert "plain text" in _decode_html_response(_raw_from_httpx(resp))

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
        result = _decode_html_response(_raw_from_httpx(resp))
        assert isinstance(result, str)

    @pytest.mark.asyncio
    async def test_acquirer_handles_shift_jis_html(self) -> None:
        """ArticleHtmlAcquirer が Shift_JIS の HTML を文字化けなく抽出する。"""
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

        acquirer = ArticleHtmlAcquirer()
        with _patch_client(client):
            result = await acquirer.acquire(
                SafeUrl("https://www.itmedia.co.jp/news/articles/test.html")
            )

        assert isinstance(result, AcquiredContent)
        assert "量子コンピューティング" in result.body


class TestExtract:
    """_acquire_content_from_response: RawResponse → ContentAcquisitionOutcome
    (同期・例外を投げない層)。

    取得 (_fetch) と切り離し、RawResponse を直接与えて content acquisition 契約だけを
    検証する。decode/parse の失敗を crash variant に畳む契約はここが正本。
    """

    def test_non_html_returns_not_html(self) -> None:
        raw = RawResponse(
            url="https://example.com/doc.pdf",
            content_type="application/pdf",
            charset_from_header=None,
            content=b"%PDF-1.4",
            decoded_text="",
        )
        result = ArticleHtmlAcquirer()._acquire_content_from_response(raw)
        assert isinstance(result, NotHtml)
        assert result.content_type == "application/pdf"

    def test_valid_html_returns_acquired_content(self) -> None:
        # trafilatura の deduplicate はモジュール跨ぎの cache を持つため、他テストと
        # 本文が衝突すると discard され ParserGaveUp になる。固有本文で隔離する。
        unique_html = (
            "<html><head><title>Extract Phase Unit Test</title></head>"
            "<body><article><h1>Isolated Extraction Sample</h1>"
            "<p>This paragraph exists solely to exercise the content acquisition "
            "phase in isolation from the fetch phase. It carries enough unique prose "
            "to clear the fifty character body quality gate while staying clear of "
            "trafilatura cross-call deduplication.</p>"
            "</article></body></html>"
        )
        raw = RawResponse(
            url="https://example.com/extract-unit",
            content_type="text/html; charset=utf-8",
            charset_from_header="utf-8",
            content=unique_html.encode("utf-8"),
            decoded_text=unique_html,
        )
        result = ArticleHtmlAcquirer()._acquire_content_from_response(raw)
        assert isinstance(result, AcquiredContent)
        assert result.title
        assert len(result.body) > 50

    def test_minimal_html_returns_quality_failure(self) -> None:
        """品質ゲート未達は ParserGaveUp か QualityGateFailed のどちらか。"""
        minimal_html = "<html><body><p>Short</p></body></html>"
        raw = RawResponse(
            url="https://example.com/short",
            content_type="text/html",
            charset_from_header=None,
            content=minimal_html.encode("utf-8"),
            decoded_text=minimal_html,
        )
        result = ArticleHtmlAcquirer()._acquire_content_from_response(raw)
        assert isinstance(result, ParserGaveUp | QualityGateFailed)
        if isinstance(result, QualityGateFailed):
            assert result.body_length < 50

    def test_parse_crash_folds_into_parse_crashed(self) -> None:
        """trafilatura 段の例外を漏らさず ``ParseCrashed`` に畳む。"""
        raw = RawResponse(
            url="https://example.com/article",
            content_type="text/html; charset=utf-8",
            charset_from_header="utf-8",
            content=SAMPLE_HTML.encode("utf-8"),
            decoded_text=SAMPLE_HTML,
        )
        with patch(
            "app.collection.article_completion.acquirer.trafilatura.bare_extraction",
            side_effect=RuntimeError("parse boom"),
        ):
            result = ArticleHtmlAcquirer()._acquire_content_from_response(raw)
        assert isinstance(result, ParseCrashed)
        assert result.error_class == "RuntimeError"

    def test_mojibake_in_body_emits_warning_log(self) -> None:
        """文字化け body は outcome を変えず ``mojibake_detected`` を warning ログ。

        Phase 1 は観測のみ: 置換文字 ``U+FFFD`` を含む本文でも結果は
        ``AcquiredContent`` のまま、ログだけが生 metric を伴って出る。
        trafilatura の正規化挙動に依存しないよう抽出結果を直接 patch する。
        """
        body_text = (
            "This readable article body easily clears the fifty character "
            "quality gate while carrying a garbled tail ��� here."
        )
        replacement_count = body_text.count("�")
        # bare_extraction が返すのと同じ実型 (TrafilaturaDocument) で patch する。
        # orchestrator が isinstance(parsed, TrafilaturaDocument) で narrow するため
        # duck-typed stub は不可。
        fake_document = TrafilaturaDocument(
            text=body_text, title="Mojibake Sample Title", date=None
        )
        raw = RawResponse(
            url="https://example.com/mojibake",
            content_type="text/html; charset=utf-8",
            charset_from_header="utf-8",
            content=b"<html></html>",
            decoded_text="<html></html>",
        )
        with (
            patch(
                "app.collection.article_completion.acquirer.trafilatura."
                "bare_extraction",
                return_value=fake_document,
            ),
            capture_logs() as logs,
        ):
            result = ArticleHtmlAcquirer()._acquire_content_from_response(raw)

        assert isinstance(result, AcquiredContent)
        mojibake_logs = [log for log in logs if log.get("event") == "mojibake_detected"]
        assert len(mojibake_logs) == 1
        assert mojibake_logs[0]["log_level"] == "warning"
        assert mojibake_logs[0]["replacement_char_count"] == replacement_count
