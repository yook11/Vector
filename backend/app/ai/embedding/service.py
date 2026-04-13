"""Embedding service — orchestration, caching, batch operations."""

from __future__ import annotations

from dataclasses import dataclass, field

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.embedding.base import BaseEmbedder
from app.ai.embedding.errors import (
    DailyQuotaExhaustedError,
    EmbeddingError,
    RateLimitError,
)
from app.ai.embedding.factory import get_embedder
from app.config import settings
from app.models.article_analysis import ArticleAnalysis
from app.models.news_article import NewsArticle

logger = structlog.get_logger(__name__)


@dataclass
class EmbedResult:
    """Result of embedding a batch of articles."""

    embedded_count: int = 0
    skipped_count: int = 0
    error_count: int = 0
    errors: list[str] = field(default_factory=list)
    embedded_ids: list[int] = field(default_factory=list)


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
