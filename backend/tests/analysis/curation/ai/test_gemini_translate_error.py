"""``GeminiCurator._translate_error`` の Stage 3 翻訳テスト。

Stage 3 が translator delegation 前に挟む独自分岐は、Pydantic ``ValidationError``
→ ``CurationResponseInvalidError`` (Layer 2-B) だけである。

SDK 例外分類の網羅は
``tests/ai_providers/gemini/test_gemini_error_translator.py`` に集約。
本ファイルでは translator delegation が経路として効いていることを smoke で確認する。
"""

from __future__ import annotations

import httpx
import pytest
from google.genai.errors import APIError

from app.ai_providers.errors import (
    AIProviderErrorResponseError,
    AIProviderErrorResponseReason,
    AIProviderTransportError,
)
from app.analysis.curation.ai.gemini import GeminiCurator
from app.analysis.curation.errors import CurationResponseInvalidError


def _api_error(status: str, message: str, code: int = 400) -> APIError:
    return APIError(
        code, {"error": {"code": code, "status": status, "message": message}}
    )


def _curator() -> GeminiCurator:
    """API key check を bypass した extractor instance。"""
    return GeminiCurator.__new__(GeminiCurator)


# 入力長の超過は共通の変換器が判定し、Stage 3 の経路でも同じ理由になる


@pytest.mark.parametrize(
    "message",
    [
        "Input exceeds context length of 1048576 tokens.",
        "Input EXCEEDS CONTEXT LENGTH",
    ],
)
def test_context_length_pattern_maps_to_input_too_long(message: str) -> None:
    exc = _api_error("INVALID_ARGUMENT", message)
    translated = _curator()._translate_error(exc)
    assert isinstance(translated, AIProviderErrorResponseError)
    assert translated.CODE == "ai_provider_error_response"
    assert translated.reason is AIProviderErrorResponseReason.INPUT_TOO_LONG


def test_deadline_exceeded_with_context_pattern_also_maps_to_input_too_long() -> None:
    """``DEADLINE_EXCEEDED`` も同分岐 (translator の status guard で許可)。"""
    exc = _api_error("DEADLINE_EXCEEDED", "Input exceeds context length", code=504)
    translated = _curator()._translate_error(exc)
    assert isinstance(translated, AIProviderErrorResponseError)
    assert translated.reason is AIProviderErrorResponseReason.INPUT_TOO_LONG


# Stage 3 specific: ValidationError → CurationResponseInvalidError


def test_validation_error_maps_to_response_invalid() -> None:
    """Pydantic ValidationError は Layer 2-B (Stage 3 工程エラー)。"""
    from pydantic import BaseModel, ValidationError

    class Sample(BaseModel):
        x: int

    try:
        Sample(x="not-an-int")  # type: ignore[arg-type]
    except ValidationError as ve:
        translated = _curator()._translate_error(ve)
        assert isinstance(translated, CurationResponseInvalidError)
        assert translated.code == "extraction_response_invalid"


# Smoke: translator delegation が経路として効いている (網羅は translator test 側)


def test_delegates_timeout_to_transport_error() -> None:
    """Stage 3 も共通の変換器で通信の失敗に分類する。"""
    translated = _curator()._translate_error(httpx.ReadTimeout("read timeout"))
    assert isinstance(translated, AIProviderTransportError)


def test_delegates_server_error_to_error_response() -> None:
    """5xx は translator 経由で失敗の応答に分類される。"""
    from google.genai import errors as genai_errors

    exc = genai_errors.ServerError(
        500, {"error": {"status": "INTERNAL", "message": "boom"}}
    )
    translated = _curator()._translate_error(exc)
    assert isinstance(translated, AIProviderErrorResponseError)


def test_unknown_runtime_exception_returns_raw_exc() -> None:
    """SDK 外の想定外例外は翻訳せず is exc を返す (bare re-raise 規約)。"""
    exc = RuntimeError("totally unexpected")
    translated = _curator()._translate_error(exc)
    assert translated is exc
