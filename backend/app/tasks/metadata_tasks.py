"""Metadata tasks — RSS/HN feed fetch and pending article dispatch."""

from __future__ import annotations

import structlog
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession as SQLModelAsyncSession
from taskiq import Context, TaskiqDepends

from app.models.article_analysis import ArticleAnalysis
from app.models.news_article import NewsArticle
from app.models.news_source import NewsSource
from app.services.news_fetcher import fetch_news_for_sources
from app.tasks.brokers import _FETCH_CRON, broker_metadata

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Tasks
# ---------------------------------------------------------------------------


@broker_metadata.task(
    task_name="fetch_metadata",
    timeout=300,
    max_retries=2,
    retry_on_error=True,
    schedule=[{"cron": _FETCH_CRON}],
)
async def fetch_metadata(
    source_ids: list[int] | None = None,
    ctx: Context = TaskiqDepends(),
) -> dict:
    """Batch RSS/HN metadata fetch, then dispatch pending articles."""
    logger.info("fetch_metadata_started")
    engine = ctx.state.engine

    async with SQLModelAsyncSession(engine, expire_on_commit=False) as session:
        if source_ids is not None:
            sources = list(
                (
                    await session.execute(
                        select(NewsSource).where(NewsSource.id.in_(source_ids))
                    )
                )
                .scalars()
                .all()
            )
        else:
            sources = list(
                (
                    await session.execute(
                        select(NewsSource)
                        .where(NewsSource.is_active == True)  # noqa: E712
                        .order_by(NewsSource.name)
                    )
                )
                .scalars()
                .all()
            )

        if not sources:
            logger.info("fetch_metadata_skipped", reason="no active sources")
            return {"sources_count": 0, "fetch_new": 0}

        fr = await fetch_news_for_sources(session, sources)

    await dispatch_pending.kiq()

    result = {
        "sources_count": len(sources),
        "fetch_new": fr.new_count,
        "fetch_skipped": fr.skipped_count,
        "fetch_errors": fr.error_count,
    }
    logger.info("fetch_metadata_completed", **result)
    return result


@broker_metadata.task(
    task_name="dispatch_pending",
    timeout=60,
    max_retries=1,
    retry_on_error=True,
)
async def dispatch_pending(
    ctx: Context = TaskiqDepends(),
) -> dict:
    """Scan DB for unprocessed articles and enqueue per-article tasks."""
    from app.tasks.analysis_tasks import analyze_article
    from app.tasks.content_tasks import fetch_content
    from app.tasks.embedding_tasks import generate_embedding

    engine = ctx.state.engine
    dispatched = {"fetch_content": 0, "analyze_article": 0, "generate_embedding": 0}

    async with SQLModelAsyncSession(engine) as session:
        # Query 1: articles needing content fetch
        ids = (
            (
                await session.execute(
                    select(NewsArticle.id).where(
                        NewsArticle.original_content.is_(None),
                        NewsArticle.skip_content_fetch == False,  # noqa: E712
                    )
                )
            )
            .scalars()
            .all()
        )
        for article_id in ids:
            await fetch_content.kiq(article_id)
            dispatched["fetch_content"] += 1

        # Query 2: articles needing AI analysis
        ids = (
            (
                await session.execute(
                    select(NewsArticle.id)
                    .outerjoin(
                        ArticleAnalysis,
                        ArticleAnalysis.news_article_id == NewsArticle.id,
                    )
                    .where(
                        NewsArticle.original_content.is_not(None),
                        ArticleAnalysis.id.is_(None),
                    )
                )
            )
            .scalars()
            .all()
        )
        for article_id in ids:
            await analyze_article.kiq(article_id)
            dispatched["analyze_article"] += 1

        # Query 3: articles needing embedding
        ids = (
            (
                await session.execute(
                    select(NewsArticle.id)
                    .join(
                        ArticleAnalysis,
                        ArticleAnalysis.news_article_id == NewsArticle.id,
                    )
                    .where(ArticleAnalysis.embedding.is_(None))
                )
            )
            .scalars()
            .all()
        )
        for article_id in ids:
            await generate_embedding.kiq(article_id)
            dispatched["generate_embedding"] += 1

    logger.info("dispatch_pending_completed", **dispatched)
    return dispatched
