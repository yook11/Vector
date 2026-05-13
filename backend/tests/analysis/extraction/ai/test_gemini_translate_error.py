"""``GeminiExtractor._translate_error`` の Layer 2 翻訳テスト (PR3.5-c)。

検証する性質:
- INVALID_ARGUMENT の message に context length 系パターンが含まれる場合
  ``AIProviderInputRejectedError`` (Layer 2-A、NonRetryableDropArticle) に翻訳
- それ以外の INVALID_ARGUMENT は ``AIProviderRequestInvalidError``
  (NonRetryableKeepArticle) に翻訳
- UNAUTHENTICATED 等は ``AIProviderConfigurationError`` (NonRetryableKeepArticle)
- RESOURCE_EXHAUSTED は ``AIProviderRateLimitedError`` (RetryableError)
- TimeoutError は ``AIProviderNetworkError`` (RetryableError)
- ValidationError は ``ExtractionResponseInvalidError`` (Layer 2-B、RetryableError)
- 翻訳できない exc は **そのまま return** される (``is exc``)
- 大文字小文字差を問わない (``EXCEEDS CONTEXT LENGTH`` でも検出)
"""

from __future__ import annotations

import pytest
from google.genai.errors import APIError

from app.analysis.ai_provider_errors import (
    AIProviderConfigurationError,
    AIProviderInputRejectedError,
    AIProviderNetworkError,
    AIProviderRateLimitedError,
    AIProviderRequestInvalidError,
)
from app.analysis.extraction.ai.gemini import GeminiExtractor
from app.analysis.extraction.errors import ExtractionResponseInvalidError


def _api_error(status: str, message: str, code: int = 400) -> APIError:
    return APIError(
        code, {"error": {"code": code, "status": status, "message": message}}
    )


def _extractor() -> GeminiExtractor:
    """API key check を bypass した extractor instance。"""
    return GeminiExtractor.__new__(GeminiExtractor)


@pytest.mark.parametrize(
    "message",
    [
        "Input exceeds context length of 1048576 tokens.",
        "ERROR: context_length_exceeded",
        "request exceeds the maximum number of tokens allowed",
        "input is too long for this model",
        "Input EXCEEDS CONTEXT LENGTH",  # 大小文字混在
    ],
)
def test_invalid_argument_with_context_length_pattern_maps_to_input_rejected(
    message: str,
) -> None:
    """context length 超過は内容起因 Permanent (DROP_ARTICLE 対象)。"""
    exc = _api_error("INVALID_ARGUMENT", message)
    translated = _extractor()._translate_error(exc)
    assert isinstance(translated, AIProviderInputRejectedError)
    assert translated.CODE == "ai_error_input_rejected"


def test_invalid_argument_without_context_pattern_maps_to_request_invalid() -> None:
    """非 context-length の INVALID_ARGUMENT は request bug 扱い (KEEP_ARTICLE)。"""
    exc = _api_error("INVALID_ARGUMENT", "malformed request body")
    translated = _extractor()._translate_error(exc)
    assert isinstance(translated, AIProviderRequestInvalidError)
    assert translated.CODE == "ai_error_request_invalid"


def test_deadline_exceeded_with_context_pattern_also_maps_to_input_rejected() -> None:
    """``DEADLINE_EXCEEDED`` も同分岐内のため context length 検出は同じ。"""
    exc = _api_error("DEADLINE_EXCEEDED", "Input exceeds context length", code=504)
    translated = _extractor()._translate_error(exc)
    assert isinstance(translated, AIProviderInputRejectedError)


def test_unauthenticated_status_maps_to_configuration_error() -> None:
    exc = _api_error("UNAUTHENTICATED", "API key invalid", code=401)
    translated = _extractor()._translate_error(exc)
    assert isinstance(translated, AIProviderConfigurationError)
    assert translated.CODE == "ai_error_configuration"


def test_leaked_api_key_maps_to_configuration_error() -> None:
    exc = _api_error("INVALID_ARGUMENT", "API key reported as leaked")
    translated = _extractor()._translate_error(exc)
    assert isinstance(translated, AIProviderConfigurationError)


def test_leaked_api_key_message_is_fixed_string_not_sdk_echo() -> None:
    """red-team chain γ-1: SDK の生 message は捨て、固定文言のみを保持する。"""
    sdk_message = (
        "API key AIzaSyA1B2C3D4E5F6G7H8I9J0K1L2M3N4O5P6Q has been "
        "reported as leaked at https://github.com/foo/bar/blob/abc/secrets.py"
    )
    exc = _api_error("INVALID_ARGUMENT", sdk_message)
    translated = _extractor()._translate_error(exc)

    translated_str = str(translated)
    assert (
        translated_str
        == "Gemini API key has been reported as leaked; rotate immediately"
    )
    # SDK message に含まれる secret prefix / repo path が漏れていないこと
    assert "AIza" not in translated_str
    assert "github.com" not in translated_str


def test_resource_exhausted_maps_to_rate_limited() -> None:
    exc = _api_error("RESOURCE_EXHAUSTED", "Too many requests", code=429)
    translated = _extractor()._translate_error(exc)
    assert isinstance(translated, AIProviderRateLimitedError)
    assert translated.CODE == "ai_error_rate_limited"


def test_timeout_error_maps_to_network_error() -> None:
    translated = _extractor()._translate_error(TimeoutError("read timeout"))
    assert isinstance(translated, AIProviderNetworkError)
    assert translated.CODE == "ai_error_network"


def test_validation_error_maps_to_response_invalid() -> None:
    """Pydantic ValidationError は Layer 2-B (Stage 3 工程エラー) に翻訳される。"""
    from pydantic import BaseModel, ValidationError

    class Sample(BaseModel):
        x: int

    try:
        Sample(x="not-an-int")  # type: ignore[arg-type]
    except ValidationError as ve:
        translated = _extractor()._translate_error(ve)
        assert isinstance(translated, ExtractionResponseInvalidError)
        assert translated.CODE == "extraction_response_invalid"


def test_unknown_status_returns_raw_exc() -> None:
    """既知 APIError status いずれにも該当しない exc は翻訳されず is exc を返す。"""
    exc = _api_error("WEIRD_NEW_STATUS", "Surprise!")
    translated = _extractor()._translate_error(exc)
    assert translated is exc  # _call_once が bare re-raise する経路


def test_unknown_runtime_exception_returns_raw_exc() -> None:
    """SDK 外の想定外例外も翻訳せず is exc を返す。"""
    exc = RuntimeError("totally unexpected")
    translated = _extractor()._translate_error(exc)
    assert translated is exc
