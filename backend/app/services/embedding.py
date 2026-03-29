"""Embedding service — abstract base and orchestration layer."""

from __future__ import annotations

import abc
import asyncio
from dataclasses import dataclass, field

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.article_analysis import ArticleAnalysis
from app.models.news_article import NewsArticle

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
    embedded_ids: list[int] = field(default_factory=list)


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

    @abc.abstractmethod
    async def embed_query(self, text: str) -> list[float]:
        """Generate embedding for a search query (RETRIEVAL_QUERY task type).

        Args:
            text: Search query text to embed.

        Returns:
            A list of floats with length == self.dimension.

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


async def embed_search_query(
    text: str, embedder: BaseEmbedder | None = None
) -> list[float]:
    """Embed a search query using RETRIEVAL_QUERY task type.

    Args:
        text: Search query text.
        embedder: Embedder instance; defaults to get_embedder().

    Returns:
        A list of floats representing the query embedding.

    Raises:
        EmbeddingError: If the API call fails.
    """
    if embedder is None:
        embedder = get_embedder()
    return await embedder.embed_query(text)


def _build_embed_text(article: NewsArticle) -> str:
    """Build the canonical text to embed for a news article."""
    body = article.original_content or article.original_description or ""
    return f"{article.original_title}\n{body}"


async def embed_articles(
    session: AsyncSession,
    analyses: list[ArticleAnalysis],
    embedder: BaseEmbedder | None = None,
) -> EmbedResult:
    """Embed multiple article analyses in batches and persist embeddings to DB.

    Features:
    - Configurable batch size and interval via settings
    - Adaptive throttling: on RateLimitError, batch interval doubles (cap 120s)
    - Cooldown: after COOLDOWN_SUCCESS_COUNT consecutive successes, interval resets
    - Circuit breaker: triggers only on non-rate-limit errors (true failures)

    Args:
        session: SQLAlchemy async session (commit is called at the end).
        analyses: ArticleAnalysis rows to embed. Already-embedded rows are skipped.
        embedder: Embedder instance; defaults to get_embedder().

    Returns:
        EmbedResult with embedded/skipped/error counts.
    """
    result = EmbedResult()
    batch_size = settings.embed_batch_size
    batch_interval = settings.embed_batch_interval
    max_failures = settings.embed_max_consecutive_failures

    if not analyses:
        logger.info("embed_batch_skipped", reason="no analyses provided")
        return result

    if embedder is None:
        embedder = get_embedder()

    # Filter out already-embedded analyses
    to_embed = [a for a in analyses if a.embedding is None]
    result.skipped_count = len(analyses) - len(to_embed)

    if not to_embed:
        logger.info("embed_batch_skipped", reason="all analyses already embedded")
        return result

    # Pre-load associated articles for text building
    article_ids = [a.news_article_id for a in to_embed]
    from sqlmodel import select

    stmt = select(NewsArticle).where(NewsArticle.id.in_(article_ids))
    articles_by_id: dict[int, NewsArticle] = {
        a.id: a for a in (await session.execute(stmt)).scalars().all()
    }

    consecutive_failures = 0
    current_interval = batch_interval
    success_streak = 0
    total_batches = 0

    for batch_start in range(0, len(to_embed), batch_size):
        batch = to_embed[batch_start : batch_start + batch_size]
        texts = [
            _build_embed_text(articles_by_id[a.news_article_id])
            for a in batch
            if a.news_article_id in articles_by_id
        ]

        try:
            vectors = await embedder.embed_batch(texts)
            for analysis, vector in zip(batch, vectors):
                analysis.embedding = vector
                analysis.embedding_model = "text-embedding-004"
                session.add(analysis)
                result.embedded_ids.append(analysis.news_article_id)
            await session.flush()
            result.embedded_count += len(batch)
            consecutive_failures = 0
            total_batches += 1

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
            remaining = len(to_embed) - (batch_start + len(batch))
            result.error_count += len(batch)
            result.errors.append(str(e))
            total_batches += 1
            logger.warning(
                "embed_rate_limit_stopping",
                batch_start=batch_start,
                count=len(batch),
                remaining=remaining,
                error=str(e),
            )
            break

        except EmbeddingError as e:
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
