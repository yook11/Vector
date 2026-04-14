"""Content tasks — per-article full-text extraction."""

from __future__ import annotations

import httpx
import structlog
from sqlmodel.ext.asyncio.session import AsyncSession as SQLModelAsyncSession
from taskiq import Context, TaskiqDepends

from app.models.news_article import NewsArticle
from app.services.content_extractor import (
    HEADERS,
    HTTP_TIMEOUT,
    PermanentFetchError,
    RobotsCache,
    TemporaryFetchError,
    extract_content,
)
from app.tasks.brokers import broker_content, is_last_attempt

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Tasks
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

        # Idempotency guard
        if article.original_content is not None:
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
