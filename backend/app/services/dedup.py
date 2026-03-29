"""Duplicate article detection service.

Detects semantically similar articles using pgvector cosine distance
on article_analyses.embedding.

Note: Article grouping (ArticleGroup) is removed in this step.
The detection results are logged for future use (e.g. NewsEvent).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

import structlog
from sqlalchemy import and_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.article_analysis import ArticleAnalysis
from app.models.news_article import NewsArticle

logger = structlog.get_logger(__name__)


@dataclass
class DedupResult:
    """Result of duplicate detection run."""

    processed: int = 0
    duplicates_found: int = 0


async def _find_similar_candidates(
    session: AsyncSession,
    article_id: int,
    embedding: list[float],
    published_at: object,
    threshold: float,
    time_window_days: int,
) -> list[tuple[int, float]]:
    """Find articles similar to the given article within a time window.

    Uses article_analyses.embedding for cosine distance comparison.
    Returns list of (news_article_id, distance) tuples, ordered by distance.
    """
    if published_at is None:
        return []

    window = timedelta(days=time_window_days)
    pub_start = published_at - window
    pub_end = published_at + window

    distance_expr = ArticleAnalysis.embedding.cosine_distance(embedding)

    stmt = (
        select(
            ArticleAnalysis.news_article_id,
            distance_expr.label("distance"),
        )
        .join(NewsArticle, NewsArticle.id == ArticleAnalysis.news_article_id)
        .where(
            and_(
                ArticleAnalysis.news_article_id != article_id,
                ArticleAnalysis.embedding.is_not(None),
                NewsArticle.published_at >= pub_start,
                NewsArticle.published_at <= pub_end,
                distance_expr < threshold,
            )
        )
        .order_by(text("distance"))
        .limit(5)
    )

    rows = (await session.execute(stmt)).all()
    return [(row.news_article_id, row.distance) for row in rows]


async def detect_duplicates(
    session: AsyncSession,
    article_ids: list[int],
    threshold: float | None = None,
    time_window_days: int | None = None,
) -> DedupResult:
    """Detect duplicate articles using article_analyses embeddings.

    Args:
        session: Async database session.
        article_ids: IDs of articles to check for duplicates.
        threshold: Cosine distance threshold (default from config).
        time_window_days: Time window in days (default from config).

    Returns:
        DedupResult with processing statistics.
    """
    if threshold is None:
        threshold = settings.dedup_similarity_threshold
    if time_window_days is None:
        time_window_days = settings.dedup_time_window_days

    result = DedupResult()

    if not article_ids:
        return result

    # Load analyses with embeddings for the target articles
    stmt = (
        select(ArticleAnalysis, NewsArticle.published_at)
        .join(NewsArticle, NewsArticle.id == ArticleAnalysis.news_article_id)
        .where(
            ArticleAnalysis.news_article_id.in_(article_ids),
            ArticleAnalysis.embedding.is_not(None),
        )
    )
    rows = (await session.execute(stmt)).all()

    for analysis, published_at in rows:
        result.processed += 1

        candidates = await _find_similar_candidates(
            session,
            analysis.news_article_id,
            analysis.embedding,
            published_at,
            threshold,
            time_window_days,
        )
        if not candidates:
            continue

        best_id, best_distance = candidates[0]
        logger.info(
            "dedup_match_found",
            article_id=analysis.news_article_id,
            match_id=best_id,
            distance=round(best_distance, 4),
        )
        result.duplicates_found += 1

    logger.info(
        "dedup_completed",
        processed=result.processed,
        duplicates_found=result.duplicates_found,
    )
    return result
