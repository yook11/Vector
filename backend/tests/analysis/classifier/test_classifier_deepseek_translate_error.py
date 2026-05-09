"""``DeepSeekClassifier._translate_error`` の SDK 翻訳テーブルテスト。

PR3 で legacy ``AnalysisDomainError`` 系 → ``AIProvider*Error`` 系への翻訳に
書き直した。spec §DeepSeek SDK 翻訳テーブル全行を parametrize で網羅し、
catch-all は ``return exc`` (bare re-raise guard) する。

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
from pydantic import SecretStr

from app.analysis.classifier.deepseek import DeepSeekClassifier
from app.analysis.errors.provider import (
    AIProviderConfigurationError,
    AIProviderInsufficientBalanceError,
    AIProviderNetworkError,
    AIProviderRateLimitedError,
    AIProviderRequestInvalidError,
    AIProviderServiceUnavailableError,
)
from app.config import settings


@pytest.fixture(autouse=True)
def _set_deepseek_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """settings.deepseek_api_key を test 中だけ stub。"""
    monkeypatch.setattr(settings, "deepseek_api_key", SecretStr("test-key"))


def _make_request() -> httpx.Request:
    return httpx.Request("POST", "https://api.deepseek.com/beta/chat/completions")


def _make_response(status_code: int) -> httpx.Response:
    return httpx.Response(status_code, request=_make_request())


def _make_status_error(status_code: int, msg: str = "x") -> APIStatusError:
    """``APIStatusError`` を最小構成で作る (status_code を任意指定)。"""
    return APIStatusError(msg, response=_make_response(status_code), body=None)


# ---------------------------------------------------------------------------
# Network 系 (OpenAI SDK + builtin)
# ---------------------------------------------------------------------------


def test_api_connection_error_translates_to_network() -> None:
    classifier = DeepSeekClassifier()
    exc = APIConnectionError(request=_make_request())
    translated = classifier._translate_error(exc)
    assert isinstance(translated, AIProviderNetworkError)


def test_api_timeout_error_translates_to_network() -> None:
    classifier = DeepSeekClassifier()
    exc = APITimeoutError(request=_make_request())
    translated = classifier._translate_error(exc)
    assert isinstance(translated, AIProviderNetworkError)


@pytest.mark.parametrize(
    "exc",
    [
        TimeoutError("io timeout"),
        ConnectionError("conn reset"),
        OSError("dns failure"),
    ],
)
def test_builtin_network_errors_translate_to_network(exc: Exception) -> None:
    classifier = DeepSeekClassifier()
    translated = classifier._translate_error(exc)
    assert isinstance(translated, AIProviderNetworkError)


# ---------------------------------------------------------------------------
# Configuration 系: Auth / Permission / NotFound
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "exc_factory",
    [
        lambda: AuthenticationError("bad key", response=_make_response(401), body=None),
        lambda: PermissionDeniedError(
            "denied", response=_make_response(403), body=None
        ),
        lambda: NotFoundError("missing", response=_make_response(404), body=None),
    ],
)
def test_configuration_errors_translation(exc_factory) -> None:
    classifier = DeepSeekClassifier()
    exc = exc_factory()
    translated = classifier._translate_error(exc)
    assert isinstance(translated, AIProviderConfigurationError)


# ---------------------------------------------------------------------------
# Insufficient Balance: HTTP 402 (RateLimitError より先に評価される)
# ---------------------------------------------------------------------------


def test_status_402_translates_to_insufficient_balance() -> None:
    classifier = DeepSeekClassifier()
    exc = _make_status_error(402, "Insufficient Balance")
    translated = classifier._translate_error(exc)
    assert isinstance(translated, AIProviderInsufficientBalanceError)


# ---------------------------------------------------------------------------
# RateLimited: HTTP 429
# ---------------------------------------------------------------------------


def test_rate_limit_error_translates_to_rate_limited() -> None:
    classifier = DeepSeekClassifier()
    exc = OpenAIRateLimitError("rate limit", response=_make_response(429), body=None)
    translated = classifier._translate_error(exc)
    assert isinstance(translated, AIProviderRateLimitedError)


# ---------------------------------------------------------------------------
# RequestInvalid: BadRequest / UnprocessableEntity
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "exc_factory",
    [
        lambda: BadRequestError("bad", response=_make_response(400), body=None),
        lambda: UnprocessableEntityError(
            "unprocessable", response=_make_response(422), body=None
        ),
    ],
)
def test_request_invalid_errors_translation(exc_factory) -> None:
    classifier = DeepSeekClassifier()
    exc = exc_factory()
    translated = classifier._translate_error(exc)
    assert isinstance(translated, AIProviderRequestInvalidError)


# ---------------------------------------------------------------------------
# ServiceUnavailable: 5xx
# ---------------------------------------------------------------------------


def test_internal_server_error_translates_to_service_unavailable() -> None:
    classifier = DeepSeekClassifier()
    exc = InternalServerError("server error", response=_make_response(500), body=None)
    translated = classifier._translate_error(exc)
    assert isinstance(translated, AIProviderServiceUnavailableError)


@pytest.mark.parametrize("status_code", [502, 503, 504])
def test_5xx_status_error_translates_to_service_unavailable(status_code: int) -> None:
    """``InternalServerError`` 以外の 500 系 ``APIStatusError`` 経路。"""
    classifier = DeepSeekClassifier()
    exc = _make_status_error(status_code, f"upstream {status_code}")
    translated = classifier._translate_error(exc)
    assert isinstance(translated, AIProviderServiceUnavailableError)


# ---------------------------------------------------------------------------
# Catch-all: マップ未知は exc をそのまま return (bare re-raise guard 規約)
# ---------------------------------------------------------------------------


def test_unmappable_returns_exc_unchanged() -> None:
    classifier = DeepSeekClassifier()
    original = RuntimeError("totally unknown")
    translated = classifier._translate_error(original)
    assert translated is original


def test_unmappable_status_code_returns_exc_unchanged() -> None:
    """4xx ですが上記 dispatch にハマらないコード (e.g. 418) は素通し。"""
    classifier = DeepSeekClassifier()
    exc = _make_status_error(418, "I'm a teapot")
    translated = classifier._translate_error(exc)
    assert translated is exc
