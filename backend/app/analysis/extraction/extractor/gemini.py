"""Gemini 実装の Content Extractor — Stage 3。

Prompt 文面 / model / gen_config / response schema は ``GeminiExtractionPrompt``
が SSoT。本 class は I/O 駆動 (rate limit + SDK 例外翻訳) に責務を絞る。
"""

from __future__ import annotations

import structlog
from google import genai
from google.genai.errors import APIError, ServerError
from google.genai.types import GenerateContentConfig
from pydantic import ValidationError

from app.analysis.errors import (
    AnalysisDomainError,
    ConfigurationError,
    InvalidInputError,
    NetworkError,
    ProviderError,
    RateLimitError,
    UnclassifiedError,
)
from app.analysis.extraction.domain import ExtractionResult
from app.analysis.extraction.extractor.base import BaseExtractor
from app.analysis.extraction.extractor.gemini_prompt import GeminiExtractionPrompt
from app.config import settings

logger = structlog.get_logger(__name__)


class GeminiExtractor(BaseExtractor):
    """BaseExtractor の Gemini API 実装。"""

    MODEL = GeminiExtractionPrompt.MODEL
    RPM = 100
    RPD = 1500

    def __init__(self) -> None:
        api_key = settings.gemini_api_key.get_secret_value()
        if not api_key:
            raise ConfigurationError("GEMINI_API_KEY is not configured")
        self._client = genai.Client(api_key=api_key)

    async def extract(
        self,
        title: str,
        content: str,
    ) -> ExtractionResult:
        """プロンプトを構築し API を呼び出して構造化レスポンスを返す。"""
        prompt = GeminiExtractionPrompt.render(title=title, content=content)
        return await self._call_once(prompt)

    async def _call_api(self, prompt: str) -> ExtractionResult:
        """Gemini の generate_content API を呼び出し構造化出力を受け取る。"""
        response = await self._client.aio.models.generate_content(
            model=GeminiExtractionPrompt.MODEL,
            contents=prompt,
            config=GenerateContentConfig(
                **GeminiExtractionPrompt.GEN_CONFIG,
                response_schema=GeminiExtractionPrompt.RESPONSE_SCHEMA,
            ),
        )
        parsed = response.parsed
        if not isinstance(parsed, ExtractionResult):
            raise ProviderError(
                f"Gemini did not return ExtractionResult (got {type(parsed).__name__})"
            )
        return parsed

    def _translate_error(self, exc: Exception) -> AnalysisDomainError:
        """Gemini SDK の例外を原因の所在で分類する。"""
        if isinstance(exc, ValidationError):
            return ProviderError(f"Invalid extraction result schema: {exc}")

        if isinstance(exc, APIError):
            status = exc.status or ""
            message = exc.message or ""

            if "reported as leaked" in message:
                return ConfigurationError(f"API key leaked: {message}")

            if status in (
                "UNAUTHENTICATED",
                "PERMISSION_DENIED",
                "FAILED_PRECONDITION",
                "NOT_FOUND",
            ):
                return ConfigurationError(f"{status}: {message}")

            if status in ("INVALID_ARGUMENT", "DEADLINE_EXCEEDED"):
                return InvalidInputError(f"{status}: {message}")

            if status == "RESOURCE_EXHAUSTED":
                return RateLimitError(f"{status}: {message}")

            if isinstance(exc, ServerError):
                return ProviderError(f"{status}: {message}")

            return UnclassifiedError(
                f"Unhandled APIError {exc.code} {status}: {message}"
            )

        if isinstance(exc, (TimeoutError, ConnectionError, OSError)):
            return NetworkError(f"{type(exc).__name__}: {exc}")

        return UnclassifiedError(f"{type(exc).__name__}: {exc}")
