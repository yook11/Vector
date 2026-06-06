"""``app.analysis.deepseek_error_translator`` の golden table テスト。

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

from app.analysis.ai_provider_errors import (
    AIProviderConfigurationError,
    AIProviderInsufficientBalanceError,
    AIProviderNetworkError,
    AIProviderRateLimitedError,
    AIProviderRequestInvalidError,
    AIProviderServiceUnavailableError,
)
from app.analysis.deepseek_error_translator import (
    DeepSeekStateReason,
    translate_deepseek_error,
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
    "exc_factory,expected_cls,expected_reason",
    [
        (
            lambda: APITimeoutError(request=_make_request()),
            AIProviderNetworkError,
            DeepSeekStateReason.TIMEOUT,
        ),
        (
            lambda: APIConnectionError(request=_make_request()),
            AIProviderNetworkError,
            DeepSeekStateReason.CONNECTION,
        ),
        (
            lambda: TimeoutError("t"),
            AIProviderNetworkError,
            DeepSeekStateReason.TIMEOUT,
        ),
        (
            lambda: ConnectionError("c"),
            AIProviderNetworkError,
            DeepSeekStateReason.CONNECTION,
        ),
        (
            lambda: OSError("dns"),
            AIProviderNetworkError,
            DeepSeekStateReason.CONNECTION,
        ),
        (
            lambda: AuthenticationError("k", response=_make_response(401), body=None),
            AIProviderConfigurationError,
            DeepSeekStateReason.AUTH,
        ),
        (
            lambda: PermissionDeniedError("d", response=_make_response(403), body=None),
            AIProviderConfigurationError,
            DeepSeekStateReason.PERMISSION_DENIED,
        ),
        (
            lambda: NotFoundError("m", response=_make_response(404), body=None),
            AIProviderConfigurationError,
            DeepSeekStateReason.NOT_FOUND,
        ),
        (
            lambda: _make_status_error(402, "Insufficient Balance"),
            AIProviderInsufficientBalanceError,
            DeepSeekStateReason.INSUFFICIENT_BALANCE,
        ),
        (
            lambda: OpenAIRateLimitError("r", response=_make_response(429), body=None),
            AIProviderRateLimitedError,
            DeepSeekStateReason.RATE_LIMITED,
        ),
        (
            lambda: BadRequestError("b", response=_make_response(400), body=None),
            AIProviderRequestInvalidError,
            DeepSeekStateReason.BAD_REQUEST,
        ),
        (
            lambda: UnprocessableEntityError(
                "u", response=_make_response(422), body=None
            ),
            AIProviderRequestInvalidError,
            DeepSeekStateReason.UNPROCESSABLE,
        ),
        (
            lambda: InternalServerError("s", response=_make_response(500), body=None),
            AIProviderServiceUnavailableError,
            DeepSeekStateReason.SERVER_ERROR,
        ),
        (
            lambda: _make_status_error(503, "upstream"),
            AIProviderServiceUnavailableError,
            DeepSeekStateReason.SERVER_ERROR,
        ),
    ],
)
def test_translation_carries_code_and_reason(
    exc_factory, expected_cls: type, expected_reason: object
) -> None:
    """各分岐が CODE (class) に加え DeepSeek 状態の reason を自己記述する。"""
    translated = translate_deepseek_error(exc_factory())
    assert isinstance(translated, expected_cls)
    assert translated.reason is expected_reason  # type: ignore[attr-defined]


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
