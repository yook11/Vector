"""Embedding service — abstract base and orchestration layer."""

from __future__ import annotations

import abc
import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, ClassVar

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.infra.redis.rate_limiter import (
    RateLimitExceededError as _RateLimitExceededError,
)
from app.models.article_analysis import ArticleAnalysis
from app.models.news_article import NewsArticle

if TYPE_CHECKING:
    from app.infra.redis.rate_limiter import RateLimiter

logger = structlog.get_logger(__name__)


class EmbeddingError(Exception):
    """Raised when embedding generation fails (base / unclassifiable)."""


class RateLimitError(EmbeddingError):
    """HTTP 429 — rate limit exceeded."""


class TransientError(EmbeddingError):
    """5xx, network errors, timeouts — expected to recover with time."""


class InvalidInputError(EmbeddingError):
    """4xx (except 429) — input-caused, permanent. Retry is pointless."""


class DailyQuotaExhaustedError(EmbeddingError):
    """RPD limit reached — no more requests allowed today."""


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

    Subclasses must declare these ClassVars:
    - ``MODEL``: model identifier (e.g. ``"gemini-embedding-001"``)
    - ``DIMENSION``: output vector dimension (e.g. ``768``)
    - ``RPM``: requests-per-minute limit, or ``None`` if unlimited
    - ``RPD``: requests-per-day limit, or ``None`` if unlimited

    Retry logic, delay strategy, and error-handling policy live here.
    """

    MODEL: ClassVar[str]
    DIMENSION: ClassVar[int]
    RPM: ClassVar[int | None]
    RPD: ClassVar[int | None]

    MAX_RETRIES = 3
    RETRY_BASE_DELAY = 2.0  # exponential backoff: 2, 4, 8
    RATE_LIMIT_DELAY = 10.0
    MAX_RATE_LIMIT_RETRIES = 1

    def __init_subclass__(cls, **kwargs: object) -> None:
        super().__init_subclass__(**kwargs)
        if getattr(cls, "__abstractmethods__", None):
            return
        for attr in ("MODEL", "DIMENSION", "RPM", "RPD"):
            if attr not in cls.__dict__:
                raise TypeError(f"{cls.__name__} must define ClassVar '{attr}'")

    def __init__(
        self,
        *,
        rpm_limiter: RateLimiter | None = None,
        rpd_limiter: RateLimiter | None = None,
    ) -> None:
        self._rpm_limiter = rpm_limiter
        self._rpd_limiter = rpd_limiter

    @property
    def dimension(self) -> int:
        """Output vector dimension (e.g., 768)."""
        return self.DIMENSION

    @property
    def model_name(self) -> str:
        """Model identifier (e.g., 'gemini-embedding-001')."""
        return self.MODEL

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
                # Acquire rate-limit slots (RPD first — fail fast)
                if self._rpd_limiter is not None:
                    await self._rpd_limiter.acquire()
                if self._rpm_limiter is not None:
                    await self._rpm_limiter.acquire()

                logger.info(
                    "embed_api_call",
                    model=self.model_name,
                    attempt=attempt,
                    task_type=task_type,
                    batch_size=len(contents) if isinstance(contents, list) else 1,
                )
                vectors = await self._call_api(contents, task_type)
                logger.info(
                    "embed_api_success",
                    model=self.model_name,
                    attempt=attempt,
                    count=len(vectors),
                )
                return vectors

            except EmbeddingError:
                raise
            except _RateLimitExceededError as exc:
                raise DailyQuotaExhaustedError(str(exc)) from exc
            except Exception as exc:
                last_error = exc
                error = self._translate_error(exc)

                if isinstance(error, RateLimitError):
                    rate_limit_retries += 1
                    logger.warning(
                        "embed_rate_limited",
                        model=self.model_name,
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
                        model=self.model_name,
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


def _build_limiters(
    embedder_cls: type[BaseEmbedder],
) -> dict[str, RateLimiter | None]:
    """Read ClassVars and build RateLimiter instances."""
    from app.infra.redis.cache import _get_client
    from app.infra.redis.rate_limiter import RateLimiter

    redis = _get_client()
    rpm_limiter: RateLimiter | None = None
    rpd_limiter: RateLimiter | None = None

    if embedder_cls.RPM is not None:
        rpm_limiter = RateLimiter(
            redis=redis,
            key=f"ratelimit:{embedder_cls.MODEL}:rpm",
            max_requests=embedder_cls.RPM,
            window_seconds=60,
            block=True,
        )
    if embedder_cls.RPD is not None:
        rpd_limiter = RateLimiter(
            redis=redis,
            key=f"ratelimit:{embedder_cls.MODEL}:rpd",
            max_requests=embedder_cls.RPD,
            window_seconds=86400,
            block=False,
        )
    return {"rpm_limiter": rpm_limiter, "rpd_limiter": rpd_limiter}


def get_embedder() -> BaseEmbedder:
    """Factory: return an embedder instance based on settings.ai_provider.

    Reads ClassVars (RPM, RPD) from the embedder class and builds
    RateLimiter instances to inject via the constructor.

    Raises:
        ValueError: If ai_provider is not supported.
    """
    provider = settings.ai_provider.lower()
    if provider == "gemini":
        from app.services.gemini_embedder import GeminiEmbedder

        limiters = _build_limiters(GeminiEmbedder)
        return GeminiEmbedder(**limiters)
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
    from app.infra.redis.embedding_cache import get_query_embedding, set_query_embedding

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

    Rate limiting is handled by the embedder's RateLimiter instances
    (acquired inside ``_embed_with_retry``).  This function only manages
    batching, circuit breaker, and DB persistence.

    Args:
        session: SQLAlchemy async session (commit is called at the end).
        analyses: ArticleAnalysis rows to embed. Already-embedded rows are skipped.
        embedder: Embedder instance; defaults to get_embedder().

    Returns:
        EmbedResult with embedded/skipped/error counts.
    """
    result = EmbedResult()
    batch_size = settings.embed_batch_size
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
                analysis.embedding_model = embedder.MODEL
                session.add(analysis)
                result.embedded_ids.append(analysis.news_article_id)
            await session.flush()
            result.embedded_count += len(batch)
            consecutive_failures = 0

            logger.info(
                "embed_batch_success",
                batch_start=batch_start,
                count=len(batch),
            )

        except DailyQuotaExhaustedError as e:
            remaining = len(to_embed) - batch_start
            result.error_count += remaining
            result.errors.append(str(e))
            logger.warning(
                "embed_daily_quota_exhausted",
                batch_start=batch_start,
                remaining=remaining,
                error=str(e),
            )
            break

        except RateLimitError as e:
            remaining = len(to_embed) - (batch_start + len(batch))
            result.error_count += len(batch)
            result.errors.append(str(e))
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

    await session.commit()

    logger.info(
        "embed_articles_completed",
        embedded=result.embedded_count,
        skipped=result.skipped_count,
        errors=result.error_count,
    )
    return result
