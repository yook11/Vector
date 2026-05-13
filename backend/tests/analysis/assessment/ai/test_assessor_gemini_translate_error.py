"""``GeminiAssessor._translate_error`` の smoke テスト。

Stage 4 の ``_translate_error`` は PR3 で共通 translator への 1 行 delegation に
縮退した。分類の網羅は ``tests/analysis/test_gemini_error_translator.py`` に集約。
本ファイルは delegation が経路として繋がっていることを確認するだけ。
"""

from __future__ import annotations

import httpx
import pytest
from google.genai import errors as genai_errors
from pydantic import SecretStr

from app.analysis.ai_provider_errors import (
    AIProviderNetworkError,
    AIProviderRateLimitedError,
    AIProviderServiceUnavailableError,
)
from app.analysis.assessment.ai.gemini import GeminiAssessor
from app.config import settings


@pytest.fixture(autouse=True)
def _set_gemini_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """settings.gemini_api_key を test 中だけ stub。"""
    monkeypatch.setattr(settings, "gemini_api_key", SecretStr("test-key"))


def test_delegates_network_error() -> None:
    assessor = GeminiAssessor()
    translated = assessor._translate_error(httpx.TimeoutException("timed out"))
    assert isinstance(translated, AIProviderNetworkError)


def test_delegates_rate_limited_error() -> None:
    assessor = GeminiAssessor()
    response_json = {"error": {"status": "RESOURCE_EXHAUSTED", "message": "burst"}}
    exc = genai_errors.ClientError(429, response_json)
    translated = assessor._translate_error(exc)
    assert isinstance(translated, AIProviderRateLimitedError)


def test_delegates_server_error_to_service_unavailable() -> None:
    assessor = GeminiAssessor()
    response_json = {"error": {"status": "INTERNAL", "message": "boom"}}
    exc = genai_errors.ServerError(500, response_json)
    translated = assessor._translate_error(exc)
    assert isinstance(translated, AIProviderServiceUnavailableError)


def test_unmappable_returns_exc_unchanged() -> None:
    assessor = GeminiAssessor()
    original = RuntimeError("totally unknown")
    translated = assessor._translate_error(original)
    assert translated is original
