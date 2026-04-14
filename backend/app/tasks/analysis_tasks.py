"""Analysis tasks — AI-powered article analysis."""

from __future__ import annotations

import structlog
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession as SQLModelAsyncSession
from taskiq import Context, TaskiqDepends

from app.models.article_analysis import ArticleAnalysis
from app.models.news_article import NewsArticle
from app.services.ai_analyzer import (
    AnalysisError,
)
from app.services.ai_analyzer import (
    DailyQuotaExhaustedError as AnalysisDailyQuotaError,
)
from app.services.ai_analyzer import (
    RateLimitError as AnalysisRateLimitError,
)
from app.services.ai_analyzer import (
    analyze_article as _analyze_article_svc,
)
from app.tasks.brokers import broker_analysis, is_last_attempt

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Tasks
# ---------------------------------------------------------------------------


@broker_analysis.task(
    task_name="analyze_article",
    timeout=180,
    max_retries=2,
    retry_on_error=True,
)
async def analyze_article(
    article_id: int,
    ctx: Context = TaskiqDepends(),
) -> None:
    """Run AI analysis on a single article."""
    from app.tasks.embedding_tasks import generate_embedding

    engine = ctx.state.engine

    async with SQLModelAsyncSession(engine, expire_on_commit=False) as session:
        # Idempotency guard
        existing = (
            await session.execute(
                select(ArticleAnalysis).where(
                    ArticleAnalysis.news_article_id == article_id
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            return

        article = await session.get(NewsArticle, article_id)
        if article is None:
            logger.warning("analyze_article_not_found", article_id=article_id)
            return

        try:
            analysis = await _analyze_article_svc(session, article)
            if analysis is not None:
                await session.commit()
        except AnalysisDailyQuotaError:
            logger.warning(
                "analyze_article_daily_quota",
                article_id=article_id,
            )
            return
        except AnalysisRateLimitError:
            if is_last_attempt(ctx):
                article.original_content = None
                article.skip_content_fetch = True
                session.add(article)
                await session.commit()
                logger.warning("analyze_article_max_retries", article_id=article_id)
                return
            raise
        except AnalysisError as e:
            # Safety block or permanent AI failure
            await session.rollback()
            article.original_content = None
            article.skip_content_fetch = True
            session.add(article)
            await session.commit()
            logger.warning(
                "analyze_article_safety_block",
                article_id=article_id,
                reason=str(e),
            )
            return

    await generate_embedding.kiq(article_id)
