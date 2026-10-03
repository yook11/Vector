"""``GeminiAssessor._translate_error`` の smoke テスト。

Stage 4 の ``_translate_error`` は共通 translator への 1 行 delegation。
分類の網羅は ``tests/ai_providers/gemini/test_gemini_error_translator.py`` に集約し、
本ファイルは delegation が経路として繋がっていることだけを確認する。
"""

from __future__ import annotations

from unittest.mock import MagicMock

import httpx
from google.genai import errors as genai_errors

from app.ai_providers.errors import (
    AIProviderResponseError,
    AIProviderResponseReason,
    AIProviderTransportError,
)
from app.analysis.assessment.ai.gemini import GeminiAssessor


def _assessor() -> GeminiAssessor:
    return GeminiAssessor(client=MagicMock())


def test_delegates_transport_error() -> None:
    """SDKの通信timeoutを共通のプロバイダー通信エラーへ変換する。"""
    translated = _assessor()._translate_error(httpx.ReadTimeout("read timeout"))
    assert isinstance(translated, AIProviderTransportError)


def test_delegates_rate_limited_error() -> None:
    """SDKのレート制限を共通のプロバイダー応答エラーへ変換する。"""
    exc = genai_errors.ClientError(
        429, {"error": {"status": "RESOURCE_EXHAUSTED", "message": "rate"}}
    )
    translated = _assessor()._translate_error(exc)
    assert isinstance(translated, AIProviderResponseError)


def test_delegates_input_too_long() -> None:
    """入力長の超過は共通の変換器が判定し、Stage 4 の経路でも同じ理由になる。"""
    exc = genai_errors.ClientError(
        400,
        {
            "error": {
                "status": "INVALID_ARGUMENT",
                "message": "Input exceeds context length of 1048576 tokens.",
            }
        },
    )
    translated = _assessor()._translate_error(exc)
    assert isinstance(translated, AIProviderResponseError)
    assert translated.reason is AIProviderResponseReason.INPUT_TOO_LONG


def test_unmappable_returns_exc_unchanged() -> None:
    """分類対象外の例外は、別の例外に置き換えず同じインスタンスを返す。"""
    original = RuntimeError("totally unknown")
    translated = _assessor()._translate_error(original)
    assert translated is original
