"""Embedding service — abstract base and orchestration layer."""

from __future__ import annotations

import abc
import asyncio
from dataclasses import dataclass, field

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.news import NewsArticle

logger = structlog.get_logger(__name__)

BATCH_SIZE = 20      # articles per API request
BATCH_INTERVAL = 2.0  # seconds between batches (rate limit protection; ~30 batches/min)
MAX_CONSECUTIVE_FAILURES = 3  # abort remaining batches after this many consecutive errors


class EmbeddingError(Exception):
    """Raised when embedding generation fails."""


@dataclass
class EmbedResult:
    """Result of embedding a batch of articles."""

    embedded_count: int = 0
    skipped_count: int = 0
    error_count: int = 0
    errors: list[str] = field(default_factory=list)


class BaseEmbedder(abc.ABC):
    """Abstract base class for text embedders."""

    @abc.abstractmethod
    async def embed(self, text: str) -> list[float]:
        """Generate a dense vector embedding for a single text.

        Args:
            text: Input text to embed.

        Returns:
            A list of floats with length == self.dimension.

        Raises:
            EmbeddingError: If the API call fails after retries.
        """
        ...

    @abc.abstractmethod
    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Generate embeddings for multiple texts in a single API call.

        Args:
            texts: List of input texts to embed.

        Returns:
            A list of float lists, one per input text, each of length self.dimension.

        Raises:
            EmbeddingError: If the API call fails after retries.
        """
        ...

    @property
    @abc.abstractmethod
    def dimension(self) -> int:
        """Return the output vector dimension (e.g., 768)."""
        ...

    @property
    @abc.abstractmethod
    def provider_name(self) -> str:
        """Return the provider identifier (e.g., 'gemini')."""
        ...


def get_embedder() -> BaseEmbedder:
    """Factory: return an embedder instance based on settings.ai_provider.

    Raises:
        ValueError: If ai_provider is not supported.
    """
    provider = settings.ai_provider.lower()
    if provider == "gemini":
        from app.services.gemini_embedder import GeminiEmbedder

        return GeminiEmbedder()
    raise ValueError(f"Unsupported AI provider for embeddings: {provider}")


def _build_embed_text(article: NewsArticle) -> str:
    """Build the canonical text to embed for a news article."""
    body = article.content or article.description_original or ""
    return f"{article.title_original}\n{body}"


async def embed_articles(
    session: AsyncSession,
    articles: list[NewsArticle],
    embedder: BaseEmbedder | None = None,
) -> EmbedResult:
    """Embed multiple articles in batches and persist embeddings to DB.

    Sends BATCH_SIZE articles per API call. One batch failure counts all
    articles in that batch as errors. After MAX_CONSECUTIVE_FAILURES consecutive
    failures, remaining batches are aborted (circuit breaker).

    Args:
        session: SQLAlchemy async session (commit is called at the end).
        articles: Articles to embed. Already-embedded articles are skipped.
        embedder: Embedder instance; defaults to get_embedder().

    Returns:
        EmbedResult with embedded/skipped/error counts.
    """
    result = EmbedResult()

    if not articles:
        logger.info("embed_batch_skipped", reason="no articles provided")
        return result

    if embedder is None:
        embedder = get_embedder()

    # Filter out already-embedded articles
    to_embed = [a for a in articles if a.embedding is None]
    result.skipped_count = len(articles) - len(to_embed)

    if not to_embed:
        logger.info("embed_batch_skipped", reason="all articles already embedded")
        return result

    consecutive_failures = 0
    for batch_start in range(0, len(to_embed), BATCH_SIZE):
        batch = to_embed[batch_start : batch_start + BATCH_SIZE]
        texts = [_build_embed_text(a) for a in batch]

        try:
            vectors = await embedder.embed_batch(texts)
            for article, vector in zip(batch, vectors):
                article.embedding = vector
                session.add(article)
            await session.flush()
            result.embedded_count += len(batch)
            consecutive_failures = 0
            logger.info(
                "embed_batch_success",
                batch_start=batch_start,
                count=len(batch),
            )
        except EmbeddingError as e:
            consecutive_failures += 1
            result.error_count += len(batch)  # one batch failure = all items error (intentional)
            result.errors.append(str(e))
            # TODO: per-article fallback (embed one by one) can be added here if needed
            logger.error(
                "embed_batch_failed",
                batch_start=batch_start,
                count=len(batch),
                error=str(e),
            )
            if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                remaining = len(to_embed) - (batch_start + len(batch))
                if remaining > 0:
                    result.error_count += remaining
                    logger.warning(
                        "embed_circuit_breaker",
                        consecutive_failures=consecutive_failures,
                        remaining=remaining,
                    )
                break

        if batch_start + BATCH_SIZE < len(to_embed):
            await asyncio.sleep(BATCH_INTERVAL)

    await session.commit()

    logger.info(
        "embed_articles_completed",
        embedded=result.embedded_count,
        skipped=result.skipped_count,
        errors=result.error_count,
    )
    return result
