"""Metadata tasks — RSS/HN feed fetch and direct dispatch to downstream queues."""

from __future__ import annotations

import structlog
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession as SQLModelAsyncSession
from taskiq import Context, TaskiqDepends

from app.collection.news_fetcher import fetch_news_for_sources
from app.models.news_source import NewsSource
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

    # Dispatch new articles directly to downstream queues
    from app.tasks.analysis_tasks import analyze_article
    from app.tasks.content_tasks import fetch_content

    content_ready = set(fr.content_ready_ids)
    for article_id in fr.new_article_ids:
        if article_id in content_ready:
            await analyze_article.kiq(article_id)
        else:
            await fetch_content.kiq(article_id)

    result = {
        "sources_count": len(sources),
        "fetch_new": fr.new_count,
        "fetch_skipped": fr.skipped_count,
        "fetch_errors": fr.error_count,
    }
    logger.info("fetch_metadata_completed", **result)
    return result
