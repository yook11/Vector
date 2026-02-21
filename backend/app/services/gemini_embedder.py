"""Gemini embedding implementation using gemini-embedding-001."""

from __future__ import annotations

import asyncio

import structlog
from google import genai
from google.genai import types

from app.config import settings
from app.services.embedding import BaseEmbedder, EmbeddingError

logger = structlog.get_logger(__name__)

GEMINI_EMBED_MODEL = "gemini-embedding-001"  # truncated to 768 dims via output_dimensionality
EMBED_DIMENSION = 768
MAX_RETRIES = 3
RETRY_BASE_DELAY = 2.0  # seconds, exponential backoff: 2, 4, 8


class GeminiEmbedder(BaseEmbedder):
    """Gemini text-embedding-004 implementation of BaseEmbedder."""

    def __init__(self) -> None:
        api_key = settings.gemini_api_key
        if not api_key:
            raise EmbeddingError("GEMINI_API_KEY is not configured")
        self._client = genai.Client(api_key=api_key)

    @property
    def dimension(self) -> int:
        return EMBED_DIMENSION

    @property
    def provider_name(self) -> str:
        return "gemini"

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Embed multiple texts in a single Gemini API call.

        Args:
            texts: List of strings to embed (up to BATCH_SIZE recommended).

        Returns:
            List of float lists, one embedding per input text.

        Raises:
            EmbeddingError: If the API call fails after MAX_RETRIES.
        """
        last_error: Exception | None = None

        for attempt in range(1, MAX_RETRIES + 1):
            try:
                logger.info(
                    "gemini_embed_batch_call",
                    attempt=attempt,
                    model=GEMINI_EMBED_MODEL,
                    batch_size=len(texts),
                )
                response = await self._client.aio.models.embed_content(
                    model=GEMINI_EMBED_MODEL,
                    contents=texts,
                    config=types.EmbedContentConfig(
                        task_type="RETRIEVAL_DOCUMENT",
                        output_dimensionality=EMBED_DIMENSION,
                    ),
                )
                # response.embeddings: list[ContentEmbedding]
                # each ContentEmbedding has .values: list[float]
                vectors = [e.values for e in response.embeddings]
                logger.info(
                    "gemini_embed_batch_success",
                    attempt=attempt,
                    count=len(vectors),
                    dim=len(vectors[0]) if vectors else 0,
                )
                return vectors

            except EmbeddingError:
                raise
            except Exception as e:
                last_error = e
                logger.warning(
                    "gemini_embed_batch_error",
                    attempt=attempt,
                    max_retries=MAX_RETRIES,
                    error=str(e),
                    error_type=type(e).__name__,
                )
                if attempt < MAX_RETRIES:
                    delay = RETRY_BASE_DELAY * (2 ** (attempt - 1))
                    await asyncio.sleep(delay)

        raise EmbeddingError(
            f"Gemini embedding failed after {MAX_RETRIES} attempts: {last_error}"
        )

    async def embed(self, text: str) -> list[float]:
        """Embed a single text (thin wrapper around embed_batch)."""
        results = await self.embed_batch([text])
        return results[0]
