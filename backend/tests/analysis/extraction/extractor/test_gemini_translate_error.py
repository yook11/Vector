"""``GeminiExtractor._translate_error`` の context length 分離テスト (PR3-a-1)。

検証する性質:
- INVALID_ARGUMENT の message に context length 系パターンが含まれる場合
  ``ExtractionInputTooLargeError`` (DELETE 対象) に変換される
- それ以外の INVALID_ARGUMENT は既存 ``InvalidInputError`` を維持
- 大文字小文字差を問わない (``EXCEEDS CONTEXT LENGTH`` でも検出)
"""

from __future__ import annotations

import pytest
from google.genai.errors import APIError

from app.analysis.errors import (
    ConfigurationError,
    InvalidInputError,
    NetworkError,
    RateLimitError,
    UnclassifiedError,
)
from app.analysis.extraction.extractor.errors import ExtractionInputTooLargeError
from app.analysis.extraction.extractor.gemini import GeminiExtractor
from app.analysis.extraction.extractor.gemini_prompt import GeminiExtractionPrompt


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
def test_invalid_argument_with_context_length_pattern_maps_to_input_too_large(
    message: str,
) -> None:
    exc = _api_error("INVALID_ARGUMENT", message)
    translated = _extractor()._translate_error(exc)
    assert isinstance(translated, ExtractionInputTooLargeError)
    assert translated.prompt_version == GeminiExtractionPrompt.VERSION


def test_invalid_argument_without_context_pattern_stays_invalid_input() -> None:
    exc = _api_error("INVALID_ARGUMENT", "malformed request body")
    translated = _extractor()._translate_error(exc)
    assert isinstance(translated, InvalidInputError)
    assert not isinstance(translated, ExtractionInputTooLargeError)


def test_deadline_exceeded_with_context_pattern_also_maps_to_input_too_large() -> None:
    """``DEADLINE_EXCEEDED`` も同分岐内のため context length 検出は同じ。"""
    exc = _api_error("DEADLINE_EXCEEDED", "Input exceeds context length", code=504)
    translated = _extractor()._translate_error(exc)
    assert isinstance(translated, ExtractionInputTooLargeError)


def test_unauthenticated_status_maps_to_configuration_error() -> None:
    exc = _api_error("UNAUTHENTICATED", "API key invalid", code=401)
    translated = _extractor()._translate_error(exc)
    assert isinstance(translated, ConfigurationError)


def test_resource_exhausted_maps_to_rate_limit() -> None:
    exc = _api_error("RESOURCE_EXHAUSTED", "Too many requests", code=429)
    translated = _extractor()._translate_error(exc)
    assert isinstance(translated, RateLimitError)


def test_timeout_error_maps_to_network() -> None:
    translated = _extractor()._translate_error(TimeoutError("read timeout"))
    assert isinstance(translated, NetworkError)


def test_unknown_status_maps_to_unclassified() -> None:
    exc = _api_error("WEIRD_NEW_STATUS", "Surprise!")
    translated = _extractor()._translate_error(exc)
    assert isinstance(translated, UnclassifiedError)
