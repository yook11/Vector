"""Gemini 実装の Classifier — Stage 4。

Prompt 文面 / model / gen_config / response schema は ``GeminiClassificationPrompt``
が SSoT。本 class は I/O 駆動 (rate limit + SDK 例外翻訳) に責務を絞る。
"""

from __future__ import annotations

import structlog
from google import genai
from google.genai.errors import APIError, ServerError
from google.genai.types import GenerateContentConfig
from pydantic import ValidationError

from app.analysis.classifier.base import BaseClassifier
from app.analysis.classifier.gemini_prompt import GeminiClassificationPrompt
from app.analysis.classifier.prompts import to_domain
from app.analysis.classifier.schema import (
    AssessmentResult,
    ClassificationRawResponse,
)
from app.analysis.errors import (
    AnalysisDomainError,
    ConfigurationError,
    InvalidInputError,
    NetworkError,
    ProviderError,
    RateLimitError,
    UnclassifiedError,
)
from app.config import settings

logger = structlog.get_logger(__name__)


class GeminiClassifier(BaseClassifier):
    """BaseClassifier の Gemini API 実装。"""

    MODEL = GeminiClassificationPrompt.MODEL
    RPM = 100
    RPD = 1500

    def __init__(self) -> None:
        api_key = settings.gemini_api_key.get_secret_value()
        if not api_key:
            raise ConfigurationError("GEMINI_API_KEY is not configured")
        self._client = genai.Client(api_key=api_key)

    async def classify(
        self,
        title_ja: str,
        summary_ja: str,
    ) -> AssessmentResult:
        """Stage 3 (Extraction) の出力を判定する。原文は読まない。"""
        prompt = GeminiClassificationPrompt.render(
            title_ja=title_ja, summary_ja=summary_ja
        )
        return await self._call_once(prompt)

    async def _call_api(self, prompt: str) -> AssessmentResult:
        """Gemini の generate_content API を呼び出し構造化出力を受け取る。"""
        response = await self._client.aio.models.generate_content(
            model=GeminiClassificationPrompt.MODEL,
            contents=prompt,
            config=GenerateContentConfig(
                **GeminiClassificationPrompt.GEN_CONFIG,
                response_schema=GeminiClassificationPrompt.RESPONSE_SCHEMA,
            ),
        )
        parsed = response.parsed
        if not isinstance(parsed, ClassificationRawResponse):
            raise ProviderError(
                f"Gemini did not return ClassificationRawResponse "
                f"(got {type(parsed).__name__})"
            )
        return to_domain(parsed)

    def _translate_error(self, exc: Exception) -> AnalysisDomainError:
        """Gemini SDK の例外を原因の所在で分類する。"""
        if isinstance(exc, ValidationError):
            return ProviderError(f"Invalid classification response schema: {exc}")

        if isinstance(exc, APIError):
            status = exc.status or ""
            message = exc.message or ""

            if "reported as leaked" in message:
                # red-team chain γ-1: SDK message に key prefix が混入する経路
                # を遮断するため固定文言化する。詳細は error_chain で追える。
                return ConfigurationError(
                    "Gemini API key has been reported as leaked; rotate immediately"
                )

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
