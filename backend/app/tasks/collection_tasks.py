"""Collection tasks — RSS/HN feed fetch and per-article content extraction."""

from __future__ import annotations

import httpx
import structlog
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession as SQLModelAsyncSession
from taskiq import Context, TaskiqDepends

from app.collection.content_extractor import (
    HEADERS,
    HTTP_TIMEOUT,
    PermanentFetchError,
    RobotsCache,
    TemporaryFetchError,
    extract_content,
)
from app.collection.news_fetcher import fetch_news_for_sources
from app.models.news_article import NewsArticle
from app.models.news_source import NewsSource
from app.tasks.brokers import (
    _FETCH_CRON,
    broker_content,
    broker_metadata,
    is_last_attempt,
)

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Metadata fetch
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


# ---------------------------------------------------------------------------
# Content extraction
# ---------------------------------------------------------------------------


@broker_content.task(
    task_name="fetch_content",
    timeout=90,
    max_retries=3,
    retry_on_error=True,
)
async def fetch_content(
    article_id: int,
    ctx: Context = TaskiqDepends(),
) -> None:
    """Fetch full article content for a single article."""
    from app.tasks.analysis_tasks import analyze_article

    engine = ctx.state.engine

    async with SQLModelAsyncSession(engine) as session:
        article = await session.get(NewsArticle, article_id)
        if article is None:
            logger.warning("fetch_content_not_found", article_id=article_id)
            return

        # Idempotency guard — chain forward even if already fetched
        if article.original_content is not None:
            await analyze_article.kiq(article_id)
            return

        robots_cache = RobotsCache()
        async with httpx.AsyncClient(headers=HEADERS, timeout=HTTP_TIMEOUT) as client:
            try:
                # TODO: extract_content の引数を SafeUrl に変更する
                content = await extract_content(
                    client, str(article.original_url), robots_cache
                )
            except PermanentFetchError as e:
                article.skip_content_fetch = True
                session.add(article)
                await session.commit()
                logger.info("fetch_content_skip", article_id=article_id, reason=str(e))
                return
            except TemporaryFetchError:
                if is_last_attempt(ctx):
                    article.skip_content_fetch = True
                    session.add(article)
                    await session.commit()
                    logger.warning("fetch_content_max_retries", article_id=article_id)
                    return
                raise

        if content is None:
            article.skip_content_fetch = True
            session.add(article)
            await session.commit()
            logger.info(
                "fetch_content_skip", article_id=article_id, reason="quality_gate"
            )
            return

        article.original_content = content
        session.add(article)
        await session.commit()

    await analyze_article.kiq(article_id)
