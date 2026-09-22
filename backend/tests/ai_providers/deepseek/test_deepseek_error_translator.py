"""``app.ai_providers.deepseek.error_translator`` の golden table テスト。

DeepSeek (OpenAI SDK) の SDK 例外 / HTTP status → ``AIProvider*Error`` 分類を検証する。
各分岐は CODE (class) に加え DeepSeek 状態の ``reason`` を自己記述し、catch-all は
``return exc`` (bare re-raise guard 規約) する。

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

from app.ai_providers.deepseek.error_translator import (
    DeepSeekStateReason,
    translate_deepseek_error,
)
from app.ai_providers.errors import (
    AIProviderConfigurationError,
    AIProviderInsufficientBalanceError,
    AIProviderNetworkError,
    AIProviderRateLimitedError,
    AIProviderRequestInvalidError,
    AIProviderServiceUnavailableError,
)


def _make_request() -> httpx.Request:
    return httpx.Request("POST", "https://api.deepseek.com/beta/chat/completions")


def _make_response(status_code: int) -> httpx.Response:
    return httpx.Response(status_code, request=_make_request())


def _make_status_error(status_code: int, msg: str = "x") -> APIStatusError:
    """``APIStatusError`` を最小構成で作る (status_code を任意指定)。"""
    return APIStatusError(msg, response=_make_response(status_code), body=None)


# 全分岐 (SDK 例外種別 / HTTP status) を網羅。各行が CODE (class) と reason の両方を
# 固定するので、分類の正本テストはここ 1 本。__init__ の NOT_CONFIGURED は translator
# 分岐外 (adapter 検知) なので本テーブルには含めない。
@pytest.mark.parametrize(
    "exc_factory,expected_cls,expected_reason,expected_message",
    [
        (
            lambda: APITimeoutError(request=_make_request()),
            AIProviderNetworkError,
            DeepSeekStateReason.TIMEOUT,
            "AIプロバイダーとの通信がタイムアウトしました",
        ),
        (
            lambda: APIConnectionError(request=_make_request()),
            AIProviderNetworkError,
            DeepSeekStateReason.CONNECTION,
            "AIプロバイダーに接続できませんでした",
        ),
        (
            lambda: TimeoutError("t"),
            AIProviderNetworkError,
            DeepSeekStateReason.TIMEOUT,
            "AIプロバイダーとの通信がタイムアウトしました",
        ),
        (
            lambda: ConnectionError("c"),
            AIProviderNetworkError,
            DeepSeekStateReason.CONNECTION,
            "AIプロバイダーに接続できませんでした",
        ),
        (
            lambda: OSError("dns"),
            AIProviderNetworkError,
            DeepSeekStateReason.CONNECTION,
            "AIプロバイダーに接続できませんでした",
        ),
        (
            lambda: AuthenticationError("k", response=_make_response(401), body=None),
            AIProviderConfigurationError,
            DeepSeekStateReason.AUTH,
            "AIプロバイダーの認証に失敗しました",
        ),
        (
            lambda: PermissionDeniedError("d", response=_make_response(403), body=None),
            AIProviderConfigurationError,
            DeepSeekStateReason.PERMISSION_DENIED,
            "AIプロバイダーへのアクセス権限がありません",
        ),
        (
            lambda: NotFoundError("m", response=_make_response(404), body=None),
            AIProviderConfigurationError,
            DeepSeekStateReason.NOT_FOUND,
            "AIプロバイダーの要求先が見つかりません",
        ),
        (
            lambda: _make_status_error(402, "Insufficient Balance"),
            AIProviderInsufficientBalanceError,
            DeepSeekStateReason.INSUFFICIENT_BALANCE,
            "AIプロバイダーの利用残高が不足しています",
        ),
        (
            lambda: OpenAIRateLimitError("r", response=_make_response(429), body=None),
            AIProviderRateLimitedError,
            DeepSeekStateReason.RATE_LIMITED,
            "AIプロバイダーの呼び出し頻度の上限に達しました",
        ),
        (
            lambda: BadRequestError("b", response=_make_response(400), body=None),
            AIProviderRequestInvalidError,
            DeepSeekStateReason.BAD_REQUEST,
            "AIプロバイダーがリクエストを不正と判定しました",
        ),
        (
            lambda: UnprocessableEntityError(
                "u", response=_make_response(422), body=None
            ),
            AIProviderRequestInvalidError,
            DeepSeekStateReason.UNPROCESSABLE,
            "AIプロバイダーがリクエストを処理できませんでした",
        ),
        (
            lambda: InternalServerError("s", response=_make_response(500), body=None),
            AIProviderServiceUnavailableError,
            DeepSeekStateReason.SERVER_ERROR,
            "AIプロバイダー内部でサーバーエラーが発生しました",
        ),
        (
            lambda: _make_status_error(503, "upstream"),
            AIProviderServiceUnavailableError,
            DeepSeekStateReason.SERVER_ERROR,
            "AIプロバイダー内部でサーバーエラーが発生しました",
        ),
    ],
)
def test_translation_carries_code_reason_and_explanation(
    exc_factory, expected_cls: type, expected_reason: object, expected_message: str
) -> None:
    """SDK例外を分類し、入力値を含まない説明と理由を伝える。"""
    translated = translate_deepseek_error(exc_factory())
    assert isinstance(translated, expected_cls)
    assert translated.reason is expected_reason  # type: ignore[attr-defined]
    assert str(translated) == expected_message


def test_402_is_evaluated_before_rate_limited() -> None:
    """HTTP 402 は ``OpenAIRateLimitError`` より先に評価される (DeepSeek 固有順序)。

    402 は専用 SDK 例外がなく ``APIStatusError`` で来るため、429 系の前段に置かないと
    InsufficientBalance が RateLimited に丸められる。
    """
    translated = translate_deepseek_error(_make_status_error(402, "Insufficient"))
    assert isinstance(translated, AIProviderInsufficientBalanceError)


def test_unmappable_returns_exc_unchanged() -> None:
    original = RuntimeError("totally unknown")
    assert translate_deepseek_error(original) is original


def test_unmappable_status_code_returns_exc_unchanged() -> None:
    """4xx だが dispatch にハマらないコード (e.g. 418) は素通し。"""
    exc = _make_status_error(418, "I'm a teapot")
    assert translate_deepseek_error(exc) is exc
