"""taskiq production worker — replaces APScheduler.

Worker:    taskiq worker app.tasks.taskiq_worker:broker app.tasks.taskiq_worker
Scheduler: taskiq scheduler app.tasks.taskiq_worker:scheduler

Both require DATABASE_URL and REDIS_URL environment variables.
In Docker Compose these are set per-service; for local dev export them manually:
    export DATABASE_URL="postgresql+asyncpg://vector:vector@localhost:5433/vector"
    export REDIS_URL="redis://localhost:6379/0"

Manual task submission for testing:
    python - <<'EOF'
    import asyncio
    from app.tasks.taskiq_worker import broker, fetch_and_analyze_task
    async def main():
        await broker.startup()
        task = await fetch_and_analyze_task.kiq()
        print(f"task_id: {task.task_id}")
        result = await task.wait_result(timeout=120)
        print(f"result: {result.return_value}, err: {result.is_err}")
        await broker.shutdown()
    asyncio.run(main())
    EOF
"""

from __future__ import annotations

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
from taskiq_redis import ListQueueBroker, RedisAsyncResultBackend

from app.config import settings
from app.models.analysis import AnalysisResult
from app.models.keyword import Keyword
from app.models.news import NewsArticle
from app.services.ai_analyzer import analyze_articles
from app.services.content_extractor import extract_contents
from app.services.embedding import embed_articles
from app.services.news_fetcher import fetch_news_for_keywords

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Cron schedule derived from settings
# fetch_interval_hours must be a divisor of 24 for a regular cron expression.
# 24h uses "0 0 * * *" explicitly (*/24 is non-standard in some parsers).
# ---------------------------------------------------------------------------

_VALID_INTERVAL_HOURS = {1, 2, 3, 4, 6, 8, 12, 24}
if settings.fetch_interval_hours not in _VALID_INTERVAL_HOURS:
    raise ValueError(
        f"fetch_interval_hours={settings.fetch_interval_hours} is not a divisor of 24. "
        f"Valid values: {sorted(_VALID_INTERVAL_HOURS)}"
    )
if settings.fetch_interval_hours == 24:
    _FETCH_CRON = "0 0 * * *"  # once daily at midnight
else:
    _FETCH_CRON = f"0 */{settings.fetch_interval_hours} * * *"

# ---------------------------------------------------------------------------
# Broker and result backend
# ---------------------------------------------------------------------------

broker = (
    ListQueueBroker(url=settings.redis_url)
    .with_result_backend(
        RedisAsyncResultBackend(
            redis_url=settings.redis_url,
            result_ex_time=3600,  # keep results for 1 hour (debugging / monitoring)
        )
    )
    .with_middlewares(
        # default_retry_count=0: all tasks declare max_retries explicitly,
        # so the middleware default is intentionally unused.
        SimpleRetryMiddleware(default_retry_count=0)
    )
)

# ---------------------------------------------------------------------------
# Scheduler (separate process: taskiq scheduler app.tasks.taskiq_worker:scheduler)
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
    """Create a fresh async engine inside taskiq's event loop (loop-safe)."""
    state.engine = create_async_engine(settings.database_url, echo=False)
    # TODO(production): mask password before logging, e.g.:
    #   re.sub(r":([^:@]+)@", ":***@", settings.database_url)
    logger.info("taskiq_worker_startup", database_url=settings.database_url)


@broker.on_event(TaskiqEvents.WORKER_SHUTDOWN)
async def on_shutdown(state: TaskiqState) -> None:
    if hasattr(state, "engine"):
        await state.engine.dispose()
    logger.info("taskiq_worker_shutdown")


# ---------------------------------------------------------------------------
# Task functions
# ---------------------------------------------------------------------------


@broker.task(
    task_name="fetch_and_analyze",
    timeout=600,
    # max_retries takes precedence over SimpleRetryMiddleware.default_retry_count.
    # retry_on_error=True is required to allow SimpleRetryMiddleware to intercept.
    max_retries=3,
    retry_on_error=True,
    schedule=[{"cron": _FETCH_CRON}],
)
async def fetch_and_analyze_task(ctx: Context = TaskiqDepends()) -> dict:
    """Full news pipeline: fetch → content extraction → AI analysis.

    Uses the engine created in on_startup (shared connection pool across tasks).
    Each phase has its own try/except so a single-phase failure does not abort
    the remaining phases — matching scheduler.py's partial-success behaviour.
    retry_on_error=True handles fatal errors (e.g. DB connection failure at Phase 1).
    """
    logger.info("taskiq_task_started")
    engine = ctx.state.engine
    result: dict[str, int] = {
        "keywords_count": 0,
        "fetch_new": 0,
        "fetch_skipped": 0,
        "fetch_errors": 0,
        "content_extracted": 0,
        "content_skipped": 0,
        "content_errors": 0,
        "analyze_count": 0,
        "analyze_skipped": 0,
        "analyze_errors": 0,
        "embed_count": 0,
        "embed_skipped": 0,
        "embed_errors": 0,
    }

    # Phase 1 + 2: keywords & RSS fetch share a session to avoid detached
    # Keyword objects (fetch_news_for_keywords accesses kw.keyword, kw.id).
    async with SQLModelAsyncSession(engine, expire_on_commit=False) as session:
        # Phase 1: fetch all keywords — fatal if this fails; let retry handle it.
        keywords = list((await session.execute(select(Keyword))).scalars().all())
        result["keywords_count"] = len(keywords)
        if not keywords:
            logger.info("taskiq_task_skipped", reason="no keywords")
            return result

        # Phase 2: RSS fetch
        try:
            fr = await fetch_news_for_keywords(session, keywords)
            result["fetch_new"] = fr.new_count
            result["fetch_skipped"] = fr.skipped_count
            result["fetch_errors"] = fr.error_count
        except Exception:
            logger.exception("taskiq_fetch_phase_failed")
            result["fetch_errors"] += 1

    # Phase 3: content extraction (independent session)
    # Intentionally runs even if Phase 2 failed: processes articles already in DB
    # (content_fetched_at == None covers articles from previous successful fetches).
    try:
        async with SQLModelAsyncSession(engine, expire_on_commit=False) as session:
            no_content = list(
                (
                    await session.execute(
                        select(NewsArticle).where(
                            NewsArticle.content_fetched_at == None  # noqa: E711
                        )
                    )
                )
                .scalars()
                .all()
            )
            if no_content:
                cr = await extract_contents(session, no_content)
                result["content_extracted"] = cr.extracted_count
                result["content_skipped"] = cr.skipped_count
                result["content_errors"] = cr.error_count
            else:
                logger.info(
                    "taskiq_content_skipped", reason="all articles have content"
                )
    except Exception:
        logger.exception("taskiq_content_phase_failed")
        result["content_errors"] += 1

    # Phase 4: AI analysis (independent session)
    # Each article is committed individually inside analyze_articles, so even if
    # this session is interrupted by a timeout, already-committed results survive.
    try:
        async with SQLModelAsyncSession(engine, expire_on_commit=False) as session:
            unanalyzed = list(
                (
                    await session.execute(
                        select(NewsArticle)
                        .outerjoin(
                            AnalysisResult,
                            AnalysisResult.news_article_id == NewsArticle.id,
                        )
                        .where(AnalysisResult.id == None)  # noqa: E711
                        .order_by(NewsArticle.published_at.desc())
                        .limit(settings.max_analysis_per_run)
                    )
                )
                .scalars()
                .all()
            )
            if unanalyzed:
                ar = await analyze_articles(session, unanalyzed)
                result["analyze_count"] = ar.analyzed_count
                result["analyze_skipped"] = ar.skipped_count
                result["analyze_errors"] = ar.error_count
            else:
                logger.info("taskiq_analyze_skipped", reason="no unanalyzed articles")
    except Exception:
        logger.exception("taskiq_analyze_phase_failed")
        result["analyze_errors"] += 1

    # Phase 5: Embedding generation (independent session)
    # Runs independently of Phase 4 — covers any article with embedding IS NULL,
    # including articles from previous runs that failed to embed.
    try:
        async with SQLModelAsyncSession(engine, expire_on_commit=False) as session:
            unembedded = list(
                (
                    await session.execute(
                        select(NewsArticle).where(NewsArticle.embedding.is_(None))
                    )
                )
                .scalars()
                .all()
            )
            if unembedded:
                er = await embed_articles(session, unembedded)
                result["embed_count"] = er.embedded_count
                result["embed_skipped"] = er.skipped_count
                result["embed_errors"] = er.error_count
            else:
                logger.info(
                    "taskiq_embed_skipped", reason="all articles have embeddings"
                )
    except Exception:
        logger.exception("taskiq_embed_phase_failed")
        result["embed_errors"] += 1

    logger.info("taskiq_task_completed", **result)
    return result
