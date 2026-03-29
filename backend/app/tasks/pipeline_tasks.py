"""Pipeline tasks — chain-based news processing pipeline.

Worker:
  taskiq worker app.tasks.pipeline_tasks:broker
  app.tasks.pipeline_tasks --ack-type when_executed
Scheduler:
  taskiq scheduler app.tasks.pipeline_tasks:scheduler
"""

from __future__ import annotations

import httpx
import structlog
from sqlalchemy.ext.asyncio import create_async_engine
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession as SQLModelAsyncSession
from taskiq import (
    Context,
    SimpleRetryMiddleware,
    TaskiqDepends,
    TaskiqEvents,
    TaskiqScheduler,
    TaskiqState,
)
from taskiq.schedule_sources import LabelScheduleSource
from taskiq_redis import RedisAsyncResultBackend, RedisStreamBroker

from app.config import settings
from app.models.article_analysis import ArticleAnalysis
from app.models.news_article import NewsArticle
from app.models.news_source import NewsSource
from app.services.ai_analyzer import (
    AnalysisError,
)
from app.services.ai_analyzer import (
    RateLimitError as AnalysisRateLimitError,
)
from app.services.ai_analyzer import (
    analyze_article as _analyze_article_svc,
)
from app.services.content_extractor import (
    HEADERS,
    HTTP_TIMEOUT,
    PermanentFetchError,
    RobotsCache,
    TemporaryFetchError,
    extract_content,
)
from app.services.embedding import _build_embed_text, get_embedder
from app.services.news_fetcher import fetch_news_for_sources

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Cron schedule derived from settings
# ---------------------------------------------------------------------------

_VALID_INTERVAL_MINUTES = {5, 10, 15, 20, 30, 60}
if settings.check_interval_minutes not in _VALID_INTERVAL_MINUTES:
    raise ValueError(
        f"check_interval_minutes={settings.check_interval_minutes} "
        f"is not a divisor of 60. "
        f"Valid values: {sorted(_VALID_INTERVAL_MINUTES)}"
    )
if settings.check_interval_minutes == 60:
    _FETCH_CRON = "0 * * * *"
else:
    _FETCH_CRON = f"*/{settings.check_interval_minutes} * * * *"

# ---------------------------------------------------------------------------
# Broker and result backend
# ---------------------------------------------------------------------------

broker = (
    RedisStreamBroker(
        url=settings.redis_url,
        idle_timeout=600_000,
        maxlen=10_000,
    )
    .with_result_backend(
        RedisAsyncResultBackend(
            redis_url=settings.redis_url,
            result_ex_time=3600,
        )
    )
    .with_middlewares(SimpleRetryMiddleware(default_retry_count=0))
)

# ---------------------------------------------------------------------------
# Scheduler
# ---------------------------------------------------------------------------

scheduler = TaskiqScheduler(
    broker=broker,
    sources=[LabelScheduleSource(broker)],
)

# ---------------------------------------------------------------------------
# Lifecycle hooks
# ---------------------------------------------------------------------------


@broker.on_event(TaskiqEvents.WORKER_STARTUP)
async def on_startup(state: TaskiqState) -> None:
    state.engine = create_async_engine(settings.database_url, echo=False)
    logger.info("pipeline_worker_startup")


@broker.on_event(TaskiqEvents.WORKER_SHUTDOWN)
async def on_shutdown(state: TaskiqState) -> None:
    if hasattr(state, "engine"):
        await state.engine.dispose()
    logger.info("pipeline_worker_shutdown")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _is_last_attempt(ctx: Context) -> bool:
    """Return True if SimpleRetryMiddleware will not retry after this attempt."""
    labels = ctx.message.labels
    retry_count = int(labels.get("retry_count", 0))
    max_retries = int(labels.get("max_retries", 0))
    return retry_count >= max_retries


# ---------------------------------------------------------------------------
# Tasks
# ---------------------------------------------------------------------------


@broker.task(
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


@broker.task(
    task_name="dispatch_pending",
    timeout=60,
    max_retries=1,
    retry_on_error=True,
)
async def dispatch_pending(
    ctx: Context = TaskiqDepends(),
) -> dict:
    """Scan DB for unprocessed articles and enqueue per-article tasks."""
    engine = ctx.state.engine
    dispatched = {"fetch_content": 0, "analyze_article": 0, "generate_embedding": 0}

    async with SQLModelAsyncSession(engine) as session:
        # Query 1: articles needing content fetch
        ids = (
            (
                await session.execute(
                    select(NewsArticle.id)
                    .where(
                        NewsArticle.original_content.is_(None),
                        NewsArticle.skip_content_fetch == False,  # noqa: E712
                    )
                    .limit(100)
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
                    .limit(100)
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
                    .limit(100)
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


@broker.task(
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
                if _is_last_attempt(ctx):
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


@broker.task(
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
        except AnalysisRateLimitError:
            if _is_last_attempt(ctx):
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


@broker.task(
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
        vector = await embedder.embed(text)

        analysis.embedding = vector
        analysis.embedding_model = "text-embedding-004"
        session.add(analysis)
        await session.commit()

    logger.info("generate_embedding_completed", article_id=article_id)
