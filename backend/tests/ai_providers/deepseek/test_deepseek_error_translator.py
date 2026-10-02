"""``app.ai_providers.deepseek.error_translator`` の golden table テスト。

DeepSeek (OpenAI SDK) の SDK 例外 / HTTP status → ``AIProvider*Error`` 分類を検証する。
分類できない例外は ``return exc`` (bare re-raise guard 規約) する。

OpenAI SDK 2.32+ の status 系例外は ``response=httpx.Response(..., request=...)``
が必須 (``request`` 同梱必要)。helper を経由して構築する。
"""

from __future__ import annotations

import httpx
import pytest
from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AuthenticationError,
    BadRequestError,
    InternalServerError,
    NotFoundError,
    PermissionDeniedError,
    UnprocessableEntityError,
)
from openai import RateLimitError as OpenAIRateLimitError

from app.ai_providers.deepseek.error_translator import translate_deepseek_error
from app.ai_providers.errors import (
    AIProviderErrorResponseError,
    AIProviderErrorResponseReason,
    AIProviderRequestNotSentError,
    AIProviderRequestNotSentReason,
    AIProviderTransportError,
)
from app.http.destination_policy import HostBlockedError
from app.http.failure import (
    HttpTransportFailure,
    HttpTransportFailureReason,
    HttpTransportStage,
)


def _make_request() -> httpx.Request:
    return httpx.Request("POST", "https://api.deepseek.com/beta/chat/completions")


def _make_response(status_code: int) -> httpx.Response:
    return httpx.Response(status_code, request=_make_request())


def _make_status_error(status_code: int, msg: str = "x") -> APIStatusError:
    """``APIStatusError`` を最小構成で作る (status_code を任意指定)。"""
    return APIStatusError(msg, response=_make_response(status_code), body=None)


def _wrapped_by_sdk(error: APIConnectionError, cause: Exception) -> APIConnectionError:
    """openai SDK と同じく、送信中の例外を ``__cause__`` に持たせて包む。"""
    error.__cause__ = cause
    return error


# 送信前・通信


def test_host_blocked_cause_is_request_not_sent() -> None:
    """宛先の方針による拒否は、通信の失敗ではなく送らなかった失敗とする。"""
    exc = _wrapped_by_sdk(
        APIConnectionError(request=_make_request()), HostBlockedError("private")
    )

    translated = translate_deepseek_error(exc)

    assert isinstance(translated, AIProviderRequestNotSentError)
    assert translated.reason is AIProviderRequestNotSentReason.HOST_BLOCKED
    assert str(translated) == "AIプロバイダーへの通信が宛先の方針で拒否されました"


@pytest.mark.parametrize(
    "exc_factory,expected",
    [
        (
            lambda: _wrapped_by_sdk(
                APITimeoutError(request=_make_request()),
                httpx.ReadTimeout("timed out"),
            ),
            HttpTransportFailure(
                HttpTransportStage.RECEIVE, HttpTransportFailureReason.TIMEOUT
            ),
        ),
        (
            lambda: _wrapped_by_sdk(
                APIConnectionError(request=_make_request()),
                httpx.ConnectError("refused"),
            ),
            HttpTransportFailure(
                HttpTransportStage.CONNECT, HttpTransportFailureReason.NETWORK_IO
            ),
        ),
        (
            lambda: _wrapped_by_sdk(
                APIConnectionError(request=_make_request()),
                httpx.ProxyError("403 Forbidden"),
            ),
            HttpTransportFailure(
                HttpTransportStage.CONNECT,
                HttpTransportFailureReason.PROXY,
                proxy_status=403,
            ),
        ),
    ],
)
def test_sdk_wrapped_transport_failure_keeps_stage_and_reason(
    exc_factory, expected: HttpTransportFailure
) -> None:
    """SDK が包んだ httpx の例外を共通HTTPで分類し、段階と理由を残す。"""
    translated = translate_deepseek_error(exc_factory())

    assert isinstance(translated, AIProviderTransportError)
    assert translated.transport == expected


@pytest.mark.parametrize(
    "exc_factory,expected",
    [
        (
            lambda: APITimeoutError(request=_make_request()),
            HttpTransportFailure(
                HttpTransportStage.UNKNOWN, HttpTransportFailureReason.TIMEOUT
            ),
        ),
        (
            lambda: APIConnectionError(request=_make_request()),
            HttpTransportFailure(
                HttpTransportStage.UNKNOWN, HttpTransportFailureReason.UNKNOWN
            ),
        ),
        (
            lambda: _wrapped_by_sdk(
                APIConnectionError(request=_make_request()), RuntimeError("other")
            ),
            HttpTransportFailure(
                HttpTransportStage.UNKNOWN, HttpTransportFailureReason.UNKNOWN
            ),
        ),
    ],
)
def test_transport_failure_without_httpx_cause_has_unknown_stage(
    exc_factory, expected: HttpTransportFailure
) -> None:
    """包む前の例外を分類できなければ、段階を断定しない。"""
    translated = translate_deepseek_error(exc_factory())

    assert isinstance(translated, AIProviderTransportError)
    assert translated.transport == expected


@pytest.mark.parametrize(
    "exc",
    [TimeoutError("t"), ConnectionError("c"), OSError("no such file")],
)
def test_builtin_errors_are_not_classified(exc: Exception) -> None:
    """SDK は通信の例外を包むので、包まれていない組み込みの例外は分類せずに返す。"""
    assert translate_deepseek_error(exc) is exc


# 失敗の応答


@pytest.mark.parametrize(
    "exc_factory,expected_reason,expected_status,expected_message",
    [
        (
            lambda: AuthenticationError("k", response=_make_response(401), body=None),
            AIProviderErrorResponseReason.AUTH,
            401,
            "AIプロバイダーの認証に失敗しました",
        ),
        (
            lambda: PermissionDeniedError("d", response=_make_response(403), body=None),
            AIProviderErrorResponseReason.PERMISSION_DENIED,
            403,
            "AIプロバイダーへのアクセス権限がありません",
        ),
        (
            lambda: NotFoundError("m", response=_make_response(404), body=None),
            AIProviderErrorResponseReason.NOT_FOUND,
            404,
            "AIプロバイダーの要求先が見つかりません",
        ),
        (
            lambda: _make_status_error(402, "Insufficient Balance"),
            AIProviderErrorResponseReason.INSUFFICIENT_BALANCE,
            402,
            "AIプロバイダーの利用残高が不足しています",
        ),
        (
            lambda: OpenAIRateLimitError("r", response=_make_response(429), body=None),
            AIProviderErrorResponseReason.RATE_LIMITED,
            429,
            "AIプロバイダーの呼び出し頻度の上限に達しました",
        ),
        (
            lambda: BadRequestError("b", response=_make_response(400), body=None),
            AIProviderErrorResponseReason.INVALID_REQUEST,
            400,
            "AIプロバイダーがリクエストを不正と判定しました",
        ),
        (
            lambda: UnprocessableEntityError(
                "u", response=_make_response(422), body=None
            ),
            AIProviderErrorResponseReason.INVALID_REQUEST,
            422,
            "AIプロバイダーがリクエストを処理できませんでした",
        ),
        (
            lambda: InternalServerError("s", response=_make_response(500), body=None),
            AIProviderErrorResponseReason.SERVER_ERROR,
            500,
            "AIプロバイダー内部でサーバーエラーが発生しました",
        ),
        (
            lambda: _make_status_error(503, "upstream"),
            AIProviderErrorResponseReason.SERVER_ERROR,
            503,
            "AIプロバイダー内部でサーバーエラーが発生しました",
        ),
    ],
)
def test_status_error_is_error_response_with_reason_and_status(
    exc_factory,
    expected_reason: AIProviderErrorResponseReason,
    expected_status: int,
    expected_message: str,
) -> None:
    """SDK例外を分類し、入力値を含まない説明・理由・HTTP status を伝える。"""
    translated = translate_deepseek_error(exc_factory())

    assert isinstance(translated, AIProviderErrorResponseError)
    assert translated.reason is expected_reason
    assert translated.status_code == expected_status
    assert str(translated) == expected_message


def test_unmappable_returns_exc_unchanged() -> None:
    original = RuntimeError("totally unknown")
    assert translate_deepseek_error(original) is original


def test_unmappable_status_code_returns_exc_unchanged() -> None:
    """4xx だが dispatch にハマらないコード (e.g. 418) は素通し。"""
    exc = _make_status_error(418, "I'm a teapot")
    assert translate_deepseek_error(exc) is exc
