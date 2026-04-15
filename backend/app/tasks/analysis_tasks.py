"""Analysis tasks — AI-powered article analysis and vector embedding."""

from __future__ import annotations

import structlog
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession as SQLModelAsyncSession
from taskiq import Context, TaskiqDepends

from app.analysis import (
    AnalysisDomainError,
    _build_embed_text,
    get_embedder,
)
from app.analysis import (
    DailyQuotaExhaustedError as AnalysisDailyQuotaError,
)
from app.analysis import (
    RateLimitError as AnalysisRateLimitError,
)
from app.analysis import (
    analyze_article as _analyze_article_svc,
)
from app.models.article_analysis import ArticleAnalysis
from app.models.news_article import NewsArticle
from app.tasks.brokers import broker_analysis, broker_embedding, is_last_attempt

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Analysis
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
    engine = ctx.state.engine

    async with SQLModelAsyncSession(engine, expire_on_commit=False) as session:
        # Idempotency guard — chain forward even if already analyzed
        existing = (
            await session.execute(
                select(ArticleAnalysis).where(
                    ArticleAnalysis.news_article_id == article_id
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            await generate_embedding.kiq(article_id)
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
        except AnalysisDomainError as e:
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


# ---------------------------------------------------------------------------
# Embedding
# ---------------------------------------------------------------------------


@broker_embedding.task(
    task_name="generate_embedding",
    timeout=60,
    max_retries=2,
    retry_on_error=True,
)
async def generate_embedding(
    article_id: int,
    ctx: Context = TaskiqDepends(),
) -> None:
    """Generate vector embedding for a single article's analysis."""
    engine = ctx.state.engine

    async with SQLModelAsyncSession(engine, expire_on_commit=False) as session:
        analysis = (
            await session.execute(
                select(ArticleAnalysis).where(
                    ArticleAnalysis.news_article_id == article_id
                )
            )
        ).scalar_one_or_none()

        if analysis is None:
            logger.warning("generate_embedding_no_analysis", article_id=article_id)
            return

        # Idempotency guard
        if analysis.embedding is not None:
            return

        article = await session.get(NewsArticle, article_id)
        if article is None:
            return

        embedder = get_embedder()
        text = _build_embed_text(article)
        vector = await embedder.embed_document(text)

        analysis.embedding = vector
        analysis.embedding_model = embedder.MODEL
        session.add(analysis)
        await session.commit()

    logger.info("generate_embedding_completed", article_id=article_id)
