"""Gemini embedding implementation using gemini-embedding-001."""

from __future__ import annotations

from typing import TYPE_CHECKING

from google import genai
from google.genai import types
from google.genai.errors import ClientError

from app.analysis.embedder.base import BaseEmbedder
from app.analysis.errors import (
    AnalysisDomainError,
    InvalidInputError,
    RateLimitError,
    TransientError,
)
from app.config import settings

if TYPE_CHECKING:
    from app.infra.redis.rate_limiter import RateLimiter


class GeminiEmbedder(BaseEmbedder):
    """Gemini gemini-embedding-001 implementation of BaseEmbedder."""

    MODEL = "gemini-embedding-001"
    DIMENSION = 768
    RPM = 15  # batchEmbedContents endpoint (SDK always uses this)
    RPD = 1500

    def __init__(
        self,
        *,
        rpm_limiter: RateLimiter | None = None,
        rpd_limiter: RateLimiter | None = None,
    ) -> None:
        super().__init__(rpm_limiter=rpm_limiter, rpd_limiter=rpd_limiter)
        api_key = settings.gemini_api_key.get_secret_value()
        if not api_key:
            raise AnalysisDomainError("GEMINI_API_KEY is not configured")
        self._client = genai.Client(api_key=api_key)

    async def _call_api(
        self, contents: str | list[str], task_type: str
    ) -> list[list[float]]:
        """Call Gemini embed_content API.

        - str contents  -> embedContent  (500 RPM)
        - list contents -> batchEmbedContents (15 RPM)
        """
        response = await self._client.aio.models.embed_content(
            model=self.MODEL,
            contents=contents,
            config=types.EmbedContentConfig(
                task_type=task_type,
                output_dimensionality=self.DIMENSION,
            ),
        )
        return [e.values for e in response.embeddings]

    def _translate_error(self, exc: Exception) -> AnalysisDomainError:
        """Classify Gemini SDK exceptions into the error hierarchy."""
        if isinstance(exc, ClientError):
            if exc.code == 429:
                return RateLimitError(str(exc))
            if 400 <= exc.code < 500:
                return InvalidInputError(str(exc))
            if exc.code >= 500:
                return TransientError(str(exc))

        # Fallback: check string for common rate limit indicators
        error_str = str(exc).lower()
        if any(
            p in error_str
            for p in ("429", "resource_exhausted", "rate limit", "quota exceeded")
        ):
            return RateLimitError(str(exc))

        # Network / timeout / unknown -> transient
        if isinstance(exc, (TimeoutError, ConnectionError, OSError)):
            return TransientError(str(exc))

        return AnalysisDomainError(str(exc))
