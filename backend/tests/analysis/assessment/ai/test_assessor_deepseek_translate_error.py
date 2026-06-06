"""``DeepSeekAssessor._translate_error`` の smoke テスト。

Stage 4 の ``_translate_error`` は共通 translator への 1 行 delegation に縮退した。
分類の網羅は ``tests/analysis/test_deepseek_error_translator.py`` に集約。本ファイルは
delegation が経路として繋がっていることを確認するだけ (Gemini adapter と対称)。

OpenAI SDK の status 系例外は ``response=httpx.Response(..., request=...)`` が必須。
"""

from __future__ import annotations

import httpx
import pytest
from openai import APITimeoutError, AuthenticationError
from openai import RateLimitError as OpenAIRateLimitError
from pydantic import SecretStr

from app.analysis.ai_provider_errors import (
    AIProviderConfigurationError,
    AIProviderNetworkError,
    AIProviderRateLimitedError,
)
from app.analysis.assessment.ai.deepseek import DeepSeekAssessor
from app.config import settings


@pytest.fixture(autouse=True)
def _set_deepseek_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """settings.deepseek_api_key を test 中だけ stub。"""
    monkeypatch.setattr(settings, "deepseek_api_key", SecretStr("test-key"))


def _make_request() -> httpx.Request:
    return httpx.Request("POST", "https://api.deepseek.com/beta/chat/completions")


def test_delegates_network_error() -> None:
    assessor = DeepSeekAssessor()
    translated = assessor._translate_error(APITimeoutError(request=_make_request()))
    assert isinstance(translated, AIProviderNetworkError)


def test_delegates_configuration_error() -> None:
    assessor = DeepSeekAssessor()
    exc = AuthenticationError(
        "bad key", response=httpx.Response(401, request=_make_request()), body=None
    )
    translated = assessor._translate_error(exc)
    assert isinstance(translated, AIProviderConfigurationError)


def test_delegates_rate_limited_error() -> None:
    assessor = DeepSeekAssessor()
    exc = OpenAIRateLimitError(
        "rate", response=httpx.Response(429, request=_make_request()), body=None
    )
    translated = assessor._translate_error(exc)
    assert isinstance(translated, AIProviderRateLimitedError)


def test_unmappable_returns_exc_unchanged() -> None:
    assessor = DeepSeekAssessor()
    original = RuntimeError("totally unknown")
    translated = assessor._translate_error(original)
    assert translated is original
