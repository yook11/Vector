"""Gemini 実装の Classifier — Stage 2。"""

from __future__ import annotations

import structlog
from google import genai
from google.genai.errors import APIError, ServerError
from google.genai.types import GenerateContentConfig
from pydantic import ValidationError

from app.analysis.classifier.base import BaseClassifier
from app.analysis.classifier.prompts import CLASSIFICATION_PROMPT, to_domain
from app.analysis.classifier.schema import (
    ClassificationRawResponse,
    ClassificationResponse,
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
from app.analysis.prompt_safety import sanitize_for_untrusted_block
from app.config import settings

logger = structlog.get_logger(__name__)


class GeminiClassifier(BaseClassifier):
    """BaseClassifier の Gemini API 実装。"""

    MODEL = "gemini-2.5-flash-lite"
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
    ) -> ClassificationResponse:
        """Stage 1 の出力を分類する。原文は読まない。"""
        prompt = CLASSIFICATION_PROMPT.format(
            title_ja=sanitize_for_untrusted_block(title_ja),
            summary_ja=sanitize_for_untrusted_block(summary_ja),
        )

        return await self._call_once(prompt)

    async def _call_api(self, prompt: str) -> ClassificationResponse:
        """Gemini の generate_content API を呼び出し構造化出力を受け取る。"""
        response = await self._client.aio.models.generate_content(
            model=self.MODEL,
            contents=prompt,
            config=GenerateContentConfig(
                temperature=0.2,
                max_output_tokens=1024,
                response_mime_type="application/json",
                response_schema=ClassificationRawResponse,
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
