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

COOLDOWN_SUCCESS_COUNT = 3  # consecutive successes before resetting adaptive interval


class EmbeddingError(Exception):
    """Raised when embedding generation fails."""


class RateLimitError(EmbeddingError):
    """Raised when the API returns a rate limit error (e.g., HTTP 429).

    Subclass of EmbeddingError so existing handlers still catch it,
    but the orchestration layer can catch it specifically to apply
    adaptive throttling instead of triggering the circuit breaker.
    """


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

    Features:
    - Configurable batch size and interval via settings
    - Adaptive throttling: on RateLimitError, batch interval doubles (cap 120s)
    - Cooldown: after COOLDOWN_SUCCESS_COUNT consecutive successes, interval resets
    - Circuit breaker: triggers only on non-rate-limit errors (true failures)

    Args:
        session: SQLAlchemy async session (commit is called at the end).
        articles: Articles to embed. Already-embedded articles are skipped.
        embedder: Embedder instance; defaults to get_embedder().

    Returns:
        EmbedResult with embedded/skipped/error counts.
    """
    result = EmbedResult()
    batch_size = settings.embed_batch_size
    batch_interval = settings.embed_batch_interval
    max_failures = settings.embed_max_consecutive_failures

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
    current_interval = batch_interval  # adaptive: may increase on rate limits
    success_streak = 0  # consecutive successes for cooldown reset
    total_batches = 0

    for batch_start in range(0, len(to_embed), batch_size):
        batch = to_embed[batch_start : batch_start + batch_size]
        texts = [_build_embed_text(a) for a in batch]

        try:
            vectors = await embedder.embed_batch(texts)
            for article, vector in zip(batch, vectors):
                article.embedding = vector
                session.add(article)
            await session.flush()
            result.embedded_count += len(batch)
            consecutive_failures = 0
            total_batches += 1

            # Cooldown: reset interval after sustained success
            success_streak += 1
            cooldown_met = success_streak >= COOLDOWN_SUCCESS_COUNT
            if cooldown_met and current_interval > batch_interval:
                logger.info(
                    "embed_interval_reset",
                    previous_interval=current_interval,
                    reset_to=batch_interval,
                    success_streak=success_streak,
                )
                current_interval = batch_interval
                success_streak = 0

            logger.info(
                "embed_batch_success",
                batch_start=batch_start,
                count=len(batch),
                current_interval=current_interval,
            )

        except RateLimitError as e:
            # Rate limit: do NOT count toward circuit breaker.
            result.error_count += len(batch)
            result.errors.append(str(e))
            success_streak = 0
            total_batches += 1

            # Adaptive throttling: double the interval (capped at 120s)
            current_interval = min(current_interval * 2, 120.0)
            logger.warning(
                "embed_batch_rate_limited",
                batch_start=batch_start,
                count=len(batch),
                new_interval=current_interval,
                error=str(e),
            )
            # Wait for RPM window to reset before next batch
            await asyncio.sleep(settings.embed_rate_limit_delay)

        except EmbeddingError as e:
            # Non-rate-limit error: count toward circuit breaker
            consecutive_failures += 1
            result.error_count += len(batch)
            result.errors.append(str(e))
            success_streak = 0
            total_batches += 1
            logger.error(
                "embed_batch_failed",
                batch_start=batch_start,
                count=len(batch),
                error=str(e),
            )
            if consecutive_failures >= max_failures:
                remaining = len(to_embed) - (batch_start + len(batch))
                if remaining > 0:
                    result.error_count += remaining
                    logger.warning(
                        "embed_circuit_breaker",
                        consecutive_failures=consecutive_failures,
                        remaining=remaining,
                    )
                break

        if batch_start + batch_size < len(to_embed):
            await asyncio.sleep(current_interval)

    await session.commit()

    logger.info(
        "embed_articles_completed",
        embedded=result.embedded_count,
        skipped=result.skipped_count,
        errors=result.error_count,
        total_batches=total_batches,
        estimated_daily_requests=total_batches,
        rpd_limit=1000,
    )
    return result
