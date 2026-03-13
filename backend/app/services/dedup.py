"""Duplicate article detection service (3B-1).

Detects semantically similar articles using pgvector cosine distance
and groups them into ArticleGroup clusters.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

import structlog
from sqlalchemy import and_, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.article_group import ArticleGroup
from app.models.news import NewsArticle

logger = structlog.get_logger(__name__)


@dataclass
class DedupResult:
    """Result of duplicate detection run."""

    processed: int = 0
    grouped: int = 0
    new_groups: int = 0


async def _find_similar_candidates(
    session: AsyncSession,
    article: NewsArticle,
    threshold: float,
    time_window_days: int,
) -> list[tuple[int, float, int | None]]:
    """Find articles similar to the given article within a time window.

    Returns list of (article_id, distance, article_group_id) tuples,
    ordered by distance ascending.
    """
    if article.embedding is None or article.published_at is None:
        return []

    window = timedelta(days=time_window_days)
    pub_start = article.published_at - window
    pub_end = article.published_at + window

    # Use cosine distance operator (<=>)
    distance_expr = NewsArticle.embedding.cosine_distance(article.embedding)

    stmt = (
        select(
            NewsArticle.id,
            distance_expr.label("distance"),
            NewsArticle.article_group_id,
        )
        .where(
            and_(
                NewsArticle.id != article.id,
                NewsArticle.embedding.is_not(None),
                NewsArticle.published_at >= pub_start,
                NewsArticle.published_at <= pub_end,
                distance_expr < threshold,
            )
        )
        .order_by(text("distance"))
        .limit(5)
    )

    rows = (await session.execute(stmt)).all()
    return [(row.id, row.distance, row.article_group_id) for row in rows]


async def _select_canonical(
    session: AsyncSession,
    group: ArticleGroup,
) -> int | None:
    """Select the best canonical article for a group.

    Priority: earliest published_at > has content > highest impact_score.
    """
    from app.models.analysis import AnalysisResult

    stmt = (
        select(
            NewsArticle.id,
            NewsArticle.published_at,
            NewsArticle.content,
            func.max(AnalysisResult.impact_score).label("max_impact"),
        )
        .outerjoin(AnalysisResult, AnalysisResult.news_article_id == NewsArticle.id)
        .where(NewsArticle.article_group_id == group.id)
        .group_by(NewsArticle.id)
        .order_by(
            # 1. Earliest published_at first (NULLs last)
            NewsArticle.published_at.asc().nulls_last(),
            # 2. Has content first
            (NewsArticle.content.is_(None)).asc(),
            # 3. Highest impact score first (NULLs last)
            func.max(AnalysisResult.impact_score).desc().nulls_last(),
        )
        .limit(1)
    )
    row = (await session.execute(stmt)).first()
    return row.id if row else None


async def _add_to_group(
    session: AsyncSession,
    article: NewsArticle,
    group_id: int,
) -> None:
    """Add an article to an existing group and update metadata."""
    article.article_group_id = group_id
    session.add(article)

    # Update article_count
    group = await session.get(ArticleGroup, group_id)
    if group is None:
        return

    count_stmt = (
        select(func.count())
        .select_from(NewsArticle)
        .where(NewsArticle.article_group_id == group_id)
    )
    group.article_count = (await session.scalar(count_stmt)) or 1

    # Re-evaluate canonical
    canonical_id = await _select_canonical(session, group)
    if canonical_id is not None:
        group.canonical_id = canonical_id

    session.add(group)


async def _create_group(
    session: AsyncSession,
    article: NewsArticle,
    match_id: int,
) -> ArticleGroup:
    """Create a new group from two articles."""
    group = ArticleGroup(article_count=2)
    session.add(group)
    await session.flush()  # get group.id

    # Assign both articles to the group
    article.article_group_id = group.id
    session.add(article)

    match_article = await session.get(NewsArticle, match_id)
    if match_article is not None:
        match_article.article_group_id = group.id
        session.add(match_article)

    # Select canonical
    canonical_id = await _select_canonical(session, group)
    if canonical_id is not None:
        group.canonical_id = canonical_id
    session.add(group)

    return group


async def detect_duplicates(
    session: AsyncSession,
    article_ids: list[int],
    threshold: float | None = None,
    time_window_days: int | None = None,
) -> DedupResult:
    """Detect and group duplicate articles.

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

    # Load articles to check
    stmt = select(NewsArticle).where(
        NewsArticle.id.in_(article_ids),
        NewsArticle.embedding.is_not(None),
        NewsArticle.article_group_id.is_(None),  # not already grouped
    )
    articles = list((await session.execute(stmt)).scalars().all())

    for article in articles:
        result.processed += 1

        candidates = await _find_similar_candidates(
            session, article, threshold, time_window_days
        )
        if not candidates:
            continue

        # Best match: closest distance
        best_id, best_distance, best_group_id = candidates[0]

        logger.info(
            "dedup_match_found",
            article_id=article.id,
            match_id=best_id,
            distance=round(best_distance, 4),
            match_group_id=best_group_id,
        )

        if best_group_id is not None:
            # Join existing group
            await _add_to_group(session, article, best_group_id)
        else:
            # Create new group
            await _create_group(session, article, best_id)
            result.new_groups += 1

        result.grouped += 1

    if result.grouped > 0:
        await session.commit()

    logger.info(
        "dedup_completed",
        processed=result.processed,
        grouped=result.grouped,
        new_groups=result.new_groups,
    )
    return result
