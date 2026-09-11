"""``DeepSeekAssessor._translate_error`` の smoke テスト。

Stage 4 の ``_translate_error`` は共通 translator への 1 行 delegation に縮退した。
分類の網羅は ``tests/ai_providers/deepseek/test_deepseek_error_translator.py`` に集約。
本ファイルは
delegation が経路として繋がっていることを確認するだけ (Gemini adapter と対称)。

OpenAI SDK の status 系例外は ``response=httpx.Response(..., request=...)`` が必須。
"""

from __future__ import annotations

from unittest.mock import MagicMock

import httpx
from openai import APITimeoutError, AuthenticationError
from openai import RateLimitError as OpenAIRateLimitError

from app.ai_providers.errors import (
    AIProviderConfigurationError,
    AIProviderNetworkError,
    AIProviderRateLimitedError,
)
from app.analysis.assessment.ai.deepseek import DeepSeekAssessor


def _make_request() -> httpx.Request:
    return httpx.Request("POST", "https://api.deepseek.com/beta/chat/completions")


def test_delegates_network_error() -> None:
    """SDKの通信timeoutを共通のプロバイダー通信エラーへ変換する。"""
    assessor = DeepSeekAssessor(MagicMock())
    translated = assessor._translate_error(APITimeoutError(request=_make_request()))
    assert isinstance(translated, AIProviderNetworkError)


def test_delegates_configuration_error() -> None:
    """SDKの認証失敗を共通のプロバイダー設定エラーへ変換する。"""
    assessor = DeepSeekAssessor(MagicMock())
    exc = AuthenticationError(
        "bad key", response=httpx.Response(401, request=_make_request()), body=None
    )
    translated = assessor._translate_error(exc)
    assert isinstance(translated, AIProviderConfigurationError)


def test_delegates_rate_limited_error() -> None:
    """SDKのレート制限を共通のプロバイダーレート制限エラーへ変換する。"""
    assessor = DeepSeekAssessor(MagicMock())
    exc = OpenAIRateLimitError(
        "rate", response=httpx.Response(429, request=_make_request()), body=None
    )
    translated = assessor._translate_error(exc)
    assert isinstance(translated, AIProviderRateLimitedError)


def test_unmappable_returns_exc_unchanged() -> None:
    """分類対象外の例外は、別の例外に置き換えず同じインスタンスを返す。"""
    assessor = DeepSeekAssessor(MagicMock())
    original = RuntimeError("totally unknown")
    translated = assessor._translate_error(original)
    assert translated is original
