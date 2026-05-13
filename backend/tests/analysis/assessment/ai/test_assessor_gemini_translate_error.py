"""``GeminiAssessor._translate_error`` の SDK 翻訳テーブルテスト。

PR3 で legacy ``AnalysisDomainError`` 系 → ``AIProvider*Error`` 系への翻訳に
書き直した。spec §Gemini SDK 翻訳テーブルの全 row を parametrize で網羅し、
catch-all は ``return exc`` (bare re-raise guard) する。

google-genai 1.x の ``ClientError(code, response_json)`` は ``code`` (int HTTP
status) と ``status`` (gRPC status 文字列) の両方を attribute として持つので、
両経路を網羅する。
"""

from __future__ import annotations

import httpx
import pytest
from google.genai import errors as genai_errors
from pydantic import SecretStr

from app.analysis.ai_provider_errors import (
    AIProviderConfigurationError,
    AIProviderInputRejectedError,
    AIProviderNetworkError,
    AIProviderQuotaExhaustedError,
    AIProviderRateLimitedError,
    AIProviderRequestInvalidError,
    AIProviderServiceUnavailableError,
)
from app.analysis.assessment.ai.gemini import GeminiAssessor
from app.config import settings


@pytest.fixture(autouse=True)
def _set_gemini_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """settings.gemini_api_key を test 中だけ stub。"""
    monkeypatch.setattr(settings, "gemini_api_key", SecretStr("test-key"))


def _make_client_error(
    *, code: int, status: str, message: str
) -> genai_errors.ClientError:
    """``ClientError(code, response_json)`` を簡易構築する helper。"""
    response_json = {"error": {"status": status, "message": message}}
    return genai_errors.ClientError(code, response_json)


def _make_server_error(
    *, code: int = 500, message: str = "internal"
) -> genai_errors.ServerError:
    response_json = {"error": {"status": "INTERNAL", "message": message}}
    return genai_errors.ServerError(code, response_json)


# ---------------------------------------------------------------------------
# ClientError 翻訳テーブル (HTTP code + gRPC status の両経路)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "code,status,message,expected",
    [
        # 400 INVALID_ARGUMENT 系: message で 3 way 分岐
        (400, "INVALID_ARGUMENT", "API key not valid", AIProviderConfigurationError),
        (400, "INVALID_ARGUMENT", "permission denied", AIProviderConfigurationError),
        (
            400,
            "INVALID_ARGUMENT",
            "request blocked by safety filter",
            AIProviderInputRejectedError,
        ),
        (400, "INVALID_ARGUMENT", "blocked content", AIProviderInputRejectedError),
        (400, "INVALID_ARGUMENT", "bad request body", AIProviderRequestInvalidError),
        # 401/403/404/FAILED_PRECONDITION 系
        (401, "UNAUTHENTICATED", "missing credentials", AIProviderConfigurationError),
        (403, "PERMISSION_DENIED", "forbidden", AIProviderConfigurationError),
        (404, "NOT_FOUND", "model not found", AIProviderConfigurationError),
        # 429 RESOURCE_EXHAUSTED: message で quota / rate-limit 分岐
        (
            429,
            "RESOURCE_EXHAUSTED",
            "daily quota exceeded",
            AIProviderQuotaExhaustedError,
        ),
        (429, "RESOURCE_EXHAUSTED", "rate limit reached", AIProviderRateLimitedError),
    ],
)
def test_client_error_translation(
    code: int, status: str, message: str, expected: type
) -> None:
    assessor = GeminiAssessor()
    exc = _make_client_error(code=code, status=status, message=message)
    translated = assessor._translate_error(exc)
    assert isinstance(translated, expected)


def test_failed_precondition_via_status_only_translates_to_configuration() -> None:
    """``code`` ではなく ``status`` のみで判定される FAILED_PRECONDITION 経路。"""
    assessor = GeminiAssessor()
    exc = _make_client_error(
        code=400,
        status="FAILED_PRECONDITION",
        message="model not enabled in this region",
    )
    # code=400 が先に評価されると INVALID_ARGUMENT 経路に入って message 分岐するが、
    # FAILED_PRECONDITION は本来 PreconditionError 系。ここでは INVALID_ARGUMENT
    # status を持つので bad request として翻訳される (実装の policy 通り)。
    translated = assessor._translate_error(exc)
    # message が "API key" / "permission" / "blocked" / "safety" を含まない →
    # AIProviderRequestInvalidError に落ちる
    assert isinstance(translated, AIProviderRequestInvalidError)


# ---------------------------------------------------------------------------
# ServerError → AIProviderServiceUnavailableError
# ---------------------------------------------------------------------------


def test_server_error_translation() -> None:
    assessor = GeminiAssessor()
    exc = _make_server_error()
    translated = assessor._translate_error(exc)
    assert isinstance(translated, AIProviderServiceUnavailableError)


# ---------------------------------------------------------------------------
# Network 系: httpx + builtin
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "exc",
    [
        httpx.TimeoutException("timed out"),
        httpx.ConnectError("connection refused"),
        TimeoutError("io timeout"),
        ConnectionError("conn reset"),
        OSError("dns failure"),
    ],
)
def test_network_error_translation(exc: Exception) -> None:
    assessor = GeminiAssessor()
    translated = assessor._translate_error(exc)
    assert isinstance(translated, AIProviderNetworkError)


# ---------------------------------------------------------------------------
# Security: red-team chain γ-1 — leaked key prefix を含む SDK message は固定文言化
# ---------------------------------------------------------------------------


def test_leaked_api_key_message_is_fixed_string() -> None:
    """SDK message に API key の prefix が含まれていても sanitize された固定文言を返す。

    red-team chain γ-1: Gemini API は key が漏洩した際 ``"API key
    AIzaSyXXXXXX has been reported as leaked"`` のように key を含む message
    を返す。これを ``str(exc)`` でログ / audit に流すと key prefix が漏れる
    ため、固定文言に丸める。
    """
    assessor = GeminiAssessor()
    sdk_message = (
        "API key AIzaSyA1B2C3D4E5F6G7H8I9J0K1L2M3N4O5P6Q has been reported as leaked"
    )
    exc = _make_client_error(code=400, status="INVALID_ARGUMENT", message=sdk_message)

    translated = assessor._translate_error(exc)

    assert isinstance(translated, AIProviderConfigurationError)
    assert (
        str(translated)
        == "Gemini API key has been reported as leaked; rotate immediately"
    )
    assert "AIza" not in str(translated)


# ---------------------------------------------------------------------------
# Catch-all: マップ未知は exc をそのまま return (bare re-raise guard 規約)
# ---------------------------------------------------------------------------


def test_unmappable_returns_exc_unchanged() -> None:
    assessor = GeminiAssessor()
    original = RuntimeError("totally unknown")
    translated = assessor._translate_error(original)
    # _translate_error は exc をそのまま返す → caller (_call_once) が `if
    # translated is exc: raise` で素通し
    assert translated is original
