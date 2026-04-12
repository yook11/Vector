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
    """Raised when embedding generation fails (base / unclassifiable)."""


class RateLimitError(EmbeddingError):
    """HTTP 429 — rate limit exceeded."""


class TransientError(EmbeddingError):
    """5xx, network errors, timeouts — expected to recover with time."""


class InvalidInputError(EmbeddingError):
    """4xx (except 429) — input-caused, permanent. Retry is pointless."""


@dataclass
class EmbedResult:
    """Result of embedding a batch of articles."""

    embedded_count: int = 0
    skipped_count: int = 0
    error_count: int = 0
    errors: list[str] = field(default_factory=list)
    embedded_ids: list[int] = field(default_factory=list)


class BaseEmbedder(abc.ABC):
    """Template Method base for text embedders.

    Subclasses implement two hooks:
    - ``_call_api``: raw SDK call (no error handling)
    - ``_translate_error``: classify SDK exceptions into the error hierarchy

    Retry logic, delay strategy, and error-handling policy live here.
    """

    MAX_RETRIES = 3
    RETRY_BASE_DELAY = 2.0  # exponential backoff: 2, 4, 8
    RATE_LIMIT_DELAY = 10.0
    MAX_RATE_LIMIT_RETRIES = 1

    def __init__(self, *, dimension: int, provider_name: str) -> None:
        self._dimension = dimension
        self._provider_name = provider_name

    @property
    def dimension(self) -> int:
        """Output vector dimension (e.g., 768)."""
        return self._dimension

    @property
    def provider_name(self) -> str:
        """Provider identifier (e.g., 'gemini')."""
        return self._provider_name

    # ── public API (concrete) ───────────────────────────────────

    async def embed_document(self, text: str) -> list[float]:
        """Embed a single document text."""
        vectors = await self._embed_with_retry(text, "RETRIEVAL_DOCUMENT")
        return vectors[0]

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """Embed multiple document texts in a single API call."""
        return await self._embed_with_retry(texts, "RETRIEVAL_DOCUMENT")

    async def embed_query(self, text: str) -> list[float]:
        """Embed a search query."""
        vectors = await self._embed_with_retry(text, "RETRIEVAL_QUERY")
        return vectors[0]

    # ── retry engine ────────────────────────────────────────────

    async def _embed_with_retry(
        self, contents: str | list[str], task_type: str
    ) -> list[list[float]]:
        """Call the provider API with two-tier retry.

        - RateLimitError: fixed delay, independent budget (max 1)
        - TransientError: exponential backoff (max MAX_RETRIES)
        - InvalidInputError / EmbeddingError: immediate raise
        """
        last_error: Exception | None = None
        attempt = 0
        rate_limit_retries = 0

        while attempt < self.MAX_RETRIES:
            attempt += 1
            try:
                logger.info(
                    "embed_api_call",
                    provider=self.provider_name,
                    attempt=attempt,
                    task_type=task_type,
                    batch_size=len(contents) if isinstance(contents, list) else 1,
                )
                vectors = await self._call_api(contents, task_type)
                logger.info(
                    "embed_api_success",
                    provider=self.provider_name,
                    attempt=attempt,
                    count=len(vectors),
                )
                return vectors

            except EmbeddingError:
                raise
            except Exception as exc:
                last_error = exc
                error = self._translate_error(exc)

                if isinstance(error, RateLimitError):
                    rate_limit_retries += 1
                    logger.warning(
                        "embed_rate_limited",
                        provider=self.provider_name,
                        attempt=attempt,
                        rate_limit_retry=rate_limit_retries,
                        delay_seconds=self.RATE_LIMIT_DELAY,
                        error=str(exc),
                    )
                    if rate_limit_retries <= self.MAX_RATE_LIMIT_RETRIES:
                        await asyncio.sleep(self.RATE_LIMIT_DELAY)
                        attempt -= 1  # don't consume normal retry budget
                        continue
                    raise error from exc

                if isinstance(error, InvalidInputError):
                    raise error from exc

                if isinstance(error, TransientError):
                    logger.warning(
                        "embed_transient_error",
                        provider=self.provider_name,
                        attempt=attempt,
                        max_retries=self.MAX_RETRIES,
                        error=str(exc),
                        error_type=type(exc).__name__,
                    )
                    if attempt < self.MAX_RETRIES:
                        delay = self.RETRY_BASE_DELAY * (2 ** (attempt - 1))
                        await asyncio.sleep(delay)
                    continue

                # Unknown error — don't retry
                raise error from exc

        raise EmbeddingError(
            f"Embedding failed after {self.MAX_RETRIES} attempts: {last_error}"
        )

    # ── abstract hooks (subclass provides) ──────────────────────

    @abc.abstractmethod
    async def _call_api(
        self, contents: str | list[str], task_type: str
    ) -> list[list[float]]:
        """Call the provider SDK. Return a list of vectors.

        Must return ``list[list[float]]`` even for a single text.
        Must NOT catch exceptions — let them propagate to _embed_with_retry.
        """
        ...

    @abc.abstractmethod
    def _translate_error(self, exc: Exception) -> EmbeddingError:
        """Classify an SDK exception into the error hierarchy.

        Return (not raise) the appropriate EmbeddingError subclass.
        """
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

    Checks the Redis embedding cache first; on miss, calls the embedder and
    writes the result back to the cache. Cache failures degrade gracefully to
    a direct API call.

    Args:
        text: Search query text (expected to be pre-normalized by the caller).
        embedder: Embedder instance; defaults to get_embedder().

    Returns:
        A list of floats representing the query embedding.

    Raises:
        EmbeddingError: If the API call fails.
    """
    from app.utils.embedding_cache import get_query_embedding, set_query_embedding

    cached = await get_query_embedding(text)
    if cached is not None:
        return cached

    if embedder is None:
        embedder = get_embedder()
    vector = await embedder.embed_query(text)
    await set_query_embedding(text, vector)
    return vector


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
            vectors = await embedder.embed_documents(texts)
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
