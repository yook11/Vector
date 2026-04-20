"""Gemini 実装の Content Extractor — Stage 1。"""

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
from app.analysis.extraction.extractor.base import BaseExtractor
from app.analysis.extraction.schema import ExtractionResponse
from app.config import settings

logger = structlog.get_logger(__name__)

EXTRACTION_PROMPT = """\
You are a tech news content extractor. Your job is to extract factual \
information from English tech news articles and output structured data \
in Japanese.

Article title: {title}

Article full text:
{content}

Extract the following:

1. title_ja — Accurate Japanese translation of the article title.

2. summary_ja — A factual Japanese summary of the article.
   Include:
   - Who did what, where (subjects and actions)
   - Specific numbers: amounts, scale, dates, version numbers, performance metrics
   - Technical novelty: what is new, how it differs from existing approaches
   Do NOT include:
   - Your own judgment, evaluation, or speculation
   - Industry impact assessment
   - Investment implications or market predictions
   Reconstruct the facts written in the article accurately in Japanese.

3. entities — A structured list of named entities mentioned in the article.
   Classify each entity with a short type label \
(e.g. "company", "product", "technology", "person", "organization", \
"country", "regulation", "vulnerability", etc.).
   Rules:
   - Only extract entities explicitly mentioned in the article
   - Do NOT include generic terms ("AI", "semiconductor", etc.)
"""


class GeminiExtractor(BaseExtractor):
    """BaseExtractor の Gemini API 実装。"""

    MODEL = "gemini-2.5-flash-lite"
    RPM = 50
    RPD = 1500
    CONTENT_MAX_LENGTH = 8000

    def __init__(self) -> None:
        api_key = settings.gemini_api_key.get_secret_value()
        if not api_key:
            raise ConfigurationError("GEMINI_API_KEY is not configured")
        self._client = genai.Client(api_key=api_key)

    async def extract(
        self,
        title: str,
        content: str,
    ) -> ExtractionResponse:
        """プロンプトを構築し API を呼び出して構造化レスポンスを返す。"""
        truncated = content[: self.CONTENT_MAX_LENGTH]

        prompt = EXTRACTION_PROMPT.format(
            title=title,
            content=truncated,
        )

        return await self._call_once(prompt)

    async def _call_api(self, prompt: str) -> ExtractionResponse:
        """Gemini の generate_content API を呼び出し構造化出力を受け取る。"""
        response = await self._client.aio.models.generate_content(
            model=self.MODEL,
            contents=prompt,
            config=GenerateContentConfig(
                temperature=0.2,
                max_output_tokens=2048,
                response_mime_type="application/json",
                response_schema=ExtractionResponse,
            ),
        )
        parsed = response.parsed
        if not isinstance(parsed, ExtractionResponse):
            raise ProviderError(
                "Gemini did not return ExtractionResponse "
                f"(got {type(parsed).__name__})"
            )
        return parsed

    def _translate_error(self, exc: Exception) -> AnalysisDomainError:
        """Gemini SDK の例外を原因の所在で分類する。"""
        if isinstance(exc, ValidationError):
            return ProviderError(f"Invalid extraction response schema: {exc}")

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
