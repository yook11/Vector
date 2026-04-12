"""Gemini embedding implementation using gemini-embedding-001."""

from __future__ import annotations

from google import genai
from google.genai import types
from google.genai.errors import ClientError

from app.config import settings
from app.services.embedding import (
    BaseEmbedder,
    EmbeddingError,
    InvalidInputError,
    RateLimitError,
    TransientError,
)

GEMINI_EMBED_MODEL = "gemini-embedding-001"
EMBED_DIMENSION = 768


class GeminiEmbedder(BaseEmbedder):
    """Gemini gemini-embedding-001 implementation of BaseEmbedder."""

    def __init__(self) -> None:
        super().__init__(dimension=EMBED_DIMENSION, provider_name="gemini")
        api_key = settings.gemini_api_key.get_secret_value()
        if not api_key:
            raise EmbeddingError("GEMINI_API_KEY is not configured")
        self._client = genai.Client(api_key=api_key)

    async def _call_api(
        self, contents: str | list[str], task_type: str
    ) -> list[list[float]]:
        """Call Gemini embed_content API.

        - str contents  → embedContent  (500 RPM)
        - list contents → batchEmbedContents (15 RPM)
        """
        response = await self._client.aio.models.embed_content(
            model=GEMINI_EMBED_MODEL,
            contents=contents,
            config=types.EmbedContentConfig(
                task_type=task_type,
                output_dimensionality=EMBED_DIMENSION,
            ),
        )
        return [e.values for e in response.embeddings]

    def _translate_error(self, exc: Exception) -> EmbeddingError:
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

        # Network / timeout / unknown → transient
        if isinstance(exc, (TimeoutError, ConnectionError, OSError)):
            return TransientError(str(exc))

        return EmbeddingError(str(exc))
