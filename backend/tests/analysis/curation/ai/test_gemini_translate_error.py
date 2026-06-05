"""``GeminiCurator._translate_error`` の Stage 3 specific 翻訳テスト。

Stage 3 が translator delegation 前に挟む独自分岐:

- Pydantic ``ValidationError`` → ``CurationResponseInvalidError`` (Layer 2-B)
- context-length 超過の ``INVALID_ARGUMENT`` → ``AIProviderInputRejectedError``
  (Stage 4/5 の RequestInvalid と違う「入力が長すぎる」semantics)

SDK 例外分類の網羅は ``tests/analysis/test_gemini_error_translator.py`` に集約。
本ファイルでは translator delegation が経路として効いていることを smoke で確認する。
"""

from __future__ import annotations

import pytest
from google.genai.errors import APIError

from app.analysis.ai_provider_errors import (
    AIProviderInputRejectedError,
    AIProviderNetworkError,
    AIProviderServiceUnavailableError,
)
from app.analysis.curation.ai.gemini import GeminiCurator
from app.analysis.curation.errors import CurationResponseInvalidError
from app.analysis.gemini_error_translator import GeminiContentRejectionReason


def _api_error(status: str, message: str, code: int = 400) -> APIError:
    return APIError(
        code, {"error": {"code": code, "status": status, "message": message}}
    )


def _curator() -> GeminiCurator:
    """API key check を bypass した extractor instance。"""
    return GeminiCurator.__new__(GeminiCurator)


# Stage 3 specific: context-length → InputRejected


@pytest.mark.parametrize(
    "message",
    [
        "Input exceeds context length of 1048576 tokens.",
        "Input EXCEEDS CONTEXT LENGTH",
    ],
)
def test_context_length_pattern_maps_to_input_rejected(message: str) -> None:
    """context-length 超過は DROP_ARTICLE 対象 (Stage 3 のみがこの判定を持つ)。"""
    exc = _api_error("INVALID_ARGUMENT", message)
    translated = _curator()._translate_error(exc)
    assert isinstance(translated, AIProviderInputRejectedError)
    assert translated.CODE == "ai_error_input_rejected"
    assert translated.reason is GeminiContentRejectionReason.CONTEXT_LENGTH


def test_deadline_exceeded_with_context_pattern_also_maps_to_input_rejected() -> None:
    """``DEADLINE_EXCEEDED`` も同分岐 (translator の status guard で許可)。"""
    exc = _api_error("DEADLINE_EXCEEDED", "Input exceeds context length", code=504)
    translated = _curator()._translate_error(exc)
    assert isinstance(translated, AIProviderInputRejectedError)
    assert translated.reason is GeminiContentRejectionReason.CONTEXT_LENGTH


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


def test_delegates_timeout_to_network_error() -> None:
    """PR3 で Stage 3 も network 分類するようになった証跡 (従来は unmapped)。"""
    translated = _curator()._translate_error(TimeoutError("read timeout"))
    assert isinstance(translated, AIProviderNetworkError)


def test_delegates_server_error_to_service_unavailable() -> None:
    """5xx は translator 経由で ServiceUnavailable に分類される。"""
    from google.genai import errors as genai_errors

    exc = genai_errors.ServerError(
        500, {"error": {"status": "INTERNAL", "message": "boom"}}
    )
    translated = _curator()._translate_error(exc)
    assert isinstance(translated, AIProviderServiceUnavailableError)


def test_unknown_runtime_exception_returns_raw_exc() -> None:
    """SDK 外の想定外例外は翻訳せず is exc を返す (bare re-raise 規約)。"""
    exc = RuntimeError("totally unexpected")
    translated = _curator()._translate_error(exc)
    assert translated is exc
