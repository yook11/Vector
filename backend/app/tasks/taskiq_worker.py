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

from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy import func
from sqlalchemy.ext.asyncio import create_async_engine
from sqlmodel import or_, select
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
from app.models.news import NewsArticle
from app.models.news_source import NewsSource
from app.services.ai_analyzer import analyze_articles
from app.services.content_extractor import extract_contents
from app.services.embedding import embed_articles
from app.services.news_fetcher import fetch_news_for_sources

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Cron schedule derived from settings
# check_interval_minutes: how often the scheduler checks for sources due to fetch.
# Must be a divisor of 60 for a regular cron expression.
# ---------------------------------------------------------------------------

_VALID_INTERVAL_MINUTES = {5, 10, 15, 20, 30, 60}
if settings.check_interval_minutes not in _VALID_INTERVAL_MINUTES:
    raise ValueError(
        f"check_interval_minutes={settings.check_interval_minutes} "
        f"is not a divisor of 60. "
        f"Valid values: {sorted(_VALID_INTERVAL_MINUTES)}"
    )
if settings.check_interval_minutes == 60:
    _FETCH_CRON = "0 * * * *"  # once per hour at :00
else:
    _FETCH_CRON = f"*/{settings.check_interval_minutes} * * * *"

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
    timeout=1800,  # 30 min: 5-phase pipeline with up to 200 articles
    # max_retries takes precedence over SimpleRetryMiddleware.default_retry_count.
    # retry_on_error=True is required to allow SimpleRetryMiddleware to intercept.
    max_retries=3,
    retry_on_error=True,
    schedule=[{"cron": _FETCH_CRON}],
)
async def fetch_and_analyze_task(
    source_ids: list[int] | None = None,
    ctx: Context = TaskiqDepends(),
) -> dict:
    """Full news pipeline: fetch → content extraction → AI analysis.

    Args:
        source_ids: Optional list of source IDs to fetch. None = all due sources.

    Uses the engine created in on_startup (shared connection pool across tasks).
    Each phase has its own try/except so a single-phase failure does not abort
    the remaining phases — matching scheduler.py's partial-success behaviour.
    retry_on_error=True handles fatal errors (e.g. DB connection failure at Phase 1).
    """
    logger.info("taskiq_task_started")
    engine = ctx.state.engine
    result: dict[str, int] = {
        "sources_count": 0,
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

    # Phase 1 + 2: source query & RSS fetch share a session.
    async with SQLModelAsyncSession(engine, expire_on_commit=False) as session:
        # Phase 1: query sources due for fetch — fatal if this fails.
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
                        .where(
                            NewsSource.is_active == True,  # noqa: E712
                            or_(
                                NewsSource.next_fetch_at == None,  # noqa: E711
                                NewsSource.next_fetch_at <= func.now(),
                            ),
                        )
                        .order_by(NewsSource.next_fetch_at.asc().nulls_first())
                    )
                )
                .scalars()
                .all()
            )
        result["sources_count"] = len(sources)
        if not sources:
            logger.info("taskiq_task_skipped", reason="no sources due for fetch")
            return result

        # Phase 2: fetch articles from sources
        try:
            fr = await fetch_news_for_sources(session, sources)
            result["fetch_new"] = fr.new_count
            result["fetch_skipped"] = fr.skipped_count
            result["fetch_errors"] = fr.error_count

            # A-5: update each source's scheduling metadata
            now = datetime.now(UTC)
            for sr in fr.source_results:
                source = next((s for s in sources if s.id == sr.source_id), None)
                if source is None:
                    continue

                source.next_fetch_at = now + timedelta(
                    minutes=source.fetch_interval_minutes
                )
                source.last_fetched_at = now

                if sr.success or sr.not_modified:
                    source.consecutive_errors = 0
                    if sr.etag is not None:
                        source.etag = sr.etag
                    if sr.last_modified is not None:
                        source.last_modified_header = sr.last_modified
                else:
                    source.consecutive_errors += 1
                    source.last_error_message = sr.error_message

                session.add(source)

            await session.commit()
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
                            NewsArticle.content_fetched_at == None,  # noqa: E711
                            NewsArticle.content_fetch_attempts
                            < settings.content_max_fetch_attempts,
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
                            (AnalysisResult.news_article_id == NewsArticle.id)
                            & (
                                AnalysisResult.ai_model_id
                                == settings.default_ai_model_id
                            ),
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
                ar = await analyze_articles(
                    session,
                    unanalyzed,
                    ai_model_id=settings.default_ai_model_id,
                )
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
