"""Analysis tasks — AI-powered article analysis and vector embedding."""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession as SQLModelAsyncSession
from taskiq import Context, TaskiqDepends

from app.analysis import (
    AnalysisDomainError,
    InvalidInputError,
    _build_embed_text,
    get_analyzer,
    get_embedder,
)
from app.analysis import (
    RateLimitError as AnalysisRateLimitError,
)
from app.analysis import (
    analyze_article as _analyze_article_svc,
)
from app.analysis.rate_limiter import (
    RateLimitExceededError as _RateLimitExceededError,
)
from app.models.article_analysis import ArticleAnalysis
from app.models.news_article import NewsArticle
from app.tasks.brokers import broker_analysis, broker_embedding, is_last_attempt

if TYPE_CHECKING:
    from app.analysis.rate_limiter import RateLimiter

logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Rate limiter construction
# ---------------------------------------------------------------------------


def _build_limiters(
    model: str,
    rpm: int | None,
    rpd: int | None,
) -> tuple[RateLimiter | None, RateLimiter | None]:
    """Build RPM and RPD rate limiters for a model.

    Returns:
        (rpm_limiter, rpd_limiter) tuple. Either may be None.
    """
    from app.analysis.rate_limiter import RateLimiter
    from app.redis import get_redis

    redis = get_redis()
    rpm_limiter: RateLimiter | None = None
    rpd_limiter: RateLimiter | None = None

    if rpm is not None:
        rpm_limiter = RateLimiter(
            redis=redis,
            key=f"ratelimit:{model}:rpm",
            max_requests=rpm,
            window_seconds=60,
            block=True,
        )
    if rpd is not None:
        rpd_limiter = RateLimiter(
            redis=redis,
            key=f"ratelimit:{model}:rpd",
            max_requests=rpd,
            window_seconds=86400,
            block=False,
        )
    return rpm_limiter, rpd_limiter


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

        analyzer = get_analyzer()
        rpm_limiter, rpd_limiter = _build_limiters(
            analyzer.MODEL, analyzer.RPM, analyzer.RPD
        )

        try:
            if rpd_limiter is not None:
                await rpd_limiter.acquire()
            if rpm_limiter is not None:
                await rpm_limiter.acquire()

            analysis = await _analyze_article_svc(session, article, analyzer)
            if analysis is not None:
                await session.commit()
        except _RateLimitExceededError:
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
        rpm_limiter, rpd_limiter = _build_limiters(
            embedder.MODEL, embedder.RPM, embedder.RPD
        )
        text = _build_embed_text(article)

        try:
            if rpd_limiter is not None:
                await rpd_limiter.acquire()
            if rpm_limiter is not None:
                await rpm_limiter.acquire()

            vector = await embedder.embed_document(text)
        except _RateLimitExceededError:
            logger.warning(
                "generate_embedding_daily_quota",
                article_id=article_id,
            )
            return
        except InvalidInputError as e:
            logger.warning(
                "generate_embedding_invalid_input",
                article_id=article_id,
                reason=str(e),
            )
            return
        except AnalysisRateLimitError:
            if is_last_attempt(ctx):
                logger.warning("generate_embedding_max_retries", article_id=article_id)
                return
            raise
        except AnalysisDomainError as e:
            logger.warning(
                "generate_embedding_domain_error",
                article_id=article_id,
                reason=str(e),
            )
            return

        analysis.embedding = vector
        analysis.embedding_model = embedder.MODEL
        session.add(analysis)
        await session.commit()

    logger.info("generate_embedding_completed", article_id=article_id)
