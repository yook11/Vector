"""Gemini embedding implementation using gemini-embedding-001."""

from __future__ import annotations

import asyncio

import structlog
from google import genai
from google.genai import types
from google.genai.errors import ClientError

from app.config import settings
from app.services.embedding import BaseEmbedder, EmbeddingError, RateLimitError

logger = structlog.get_logger(__name__)

GEMINI_EMBED_MODEL = "gemini-embedding-001"
EMBED_DIMENSION = 768
MAX_RETRIES = 3
RETRY_BASE_DELAY = 2.0  # seconds, exponential backoff: 2, 4, 8
RATE_LIMIT_DELAY = 30.0  # seconds to wait on 429 before retrying
MAX_RATE_LIMIT_RETRIES = 2  # independent of normal retry budget


def _is_rate_limit_error(exc: Exception) -> bool:
    """Check if an exception represents a Gemini API rate limit (HTTP 429)."""
    if isinstance(exc, ClientError) and exc.code == 429:
        return True
    # Fallback: check string content for common rate limit indicators
    error_str = str(exc).lower()
    return any(
        pattern in error_str
        for pattern in ("429", "resource_exhausted", "rate limit", "quota exceeded")
    )


class GeminiEmbedder(BaseEmbedder):
    """Gemini gemini-embedding-001 implementation of BaseEmbedder."""

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

        Two-tier retry strategy:
        - Rate limit (429): wait RATE_LIMIT_DELAY and retry without consuming
          the normal retry budget (up to MAX_RATE_LIMIT_RETRIES times).
        - Other errors: exponential backoff (2, 4, 8 seconds).

        Args:
            texts: List of strings to embed.

        Returns:
            List of float lists, one embedding per input text.

        Raises:
            RateLimitError: If rate limit retries are exhausted.
            EmbeddingError: If the API call fails after MAX_RETRIES.
        """
        last_error: Exception | None = None
        attempt = 0
        rate_limit_retries = 0

        while attempt < MAX_RETRIES:
            attempt += 1
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

                if _is_rate_limit_error(e):
                    rate_limit_retries += 1
                    logger.warning(
                        "gemini_embed_rate_limited",
                        attempt=attempt,
                        rate_limit_retry=rate_limit_retries,
                        delay_seconds=RATE_LIMIT_DELAY,
                        error=str(e),
                    )
                    if rate_limit_retries <= MAX_RATE_LIMIT_RETRIES:
                        await asyncio.sleep(RATE_LIMIT_DELAY)
                        attempt -= 1  # don't consume normal retry budget
                        continue
                    else:
                        raise RateLimitError(
                            f"Gemini rate limit exceeded after {rate_limit_retries} "
                            f"rate-limit retries: {e}"
                        )
                else:
                    # Non-rate-limit error: standard exponential backoff
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
