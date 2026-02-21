"""Scheduler service — periodic news fetching and AI analysis."""

from dataclasses import dataclass
from datetime import datetime, timezone

import structlog
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession as SQLModelAsyncSession

from app.config import settings
from app.db import engine
from app.models.analysis import AnalysisResult
from app.models.keyword import Keyword
from app.models.news import NewsArticle
from app.services.ai_analyzer import analyze_articles
from app.services.content_extractor import extract_contents
from app.services.news_fetcher import fetch_news_for_keywords

logger = structlog.get_logger(__name__)

_scheduler: AsyncIOScheduler | None = None
_last_fetch_at: datetime | None = None


def get_last_fetch_at() -> datetime | None:
    """Return the timestamp of the last successful scheduler job."""
    return _last_fetch_at


@dataclass
class SchedulerJobResult:
    """Summary of a single scheduler job run."""

    keywords_count: int = 0
    fetch_new: int = 0
    fetch_skipped: int = 0
    fetch_errors: int = 0
    content_extracted: int = 0
    content_skipped: int = 0
    content_errors: int = 0
    analyze_count: int = 0
    analyze_skipped: int = 0
    analyze_errors: int = 0


async def run_fetch_and_analyze() -> SchedulerJobResult:
    """Execute one cycle of news fetching and AI analysis.

    Creates its own database session (cannot use FastAPI DI).
    Errors are logged but never propagated to prevent scheduler crash.
    """
    result = SchedulerJobResult()
    logger.info("scheduler_job_started")

    try:
        async with SQLModelAsyncSession(engine) as session:
            # Phase 1: Query active keywords
            stmt = select(Keyword).where(Keyword.is_active == True)  # noqa: E712
            keywords = (await session.execute(stmt)).scalars().all()
            result.keywords_count = len(keywords)

            if not keywords:
                logger.info("scheduler_job_skipped", reason="no active keywords")
                return result

            # Phase 2: Fetch news
            fetch_result = await fetch_news_for_keywords(session, list(keywords))
            result.fetch_new = fetch_result.new_count
            result.fetch_skipped = fetch_result.skipped_count
            result.fetch_errors = fetch_result.error_count

            # Phase 3: Extract content for articles without content
            no_content_stmt = select(NewsArticle).where(
                NewsArticle.content_fetched_at == None,  # noqa: E711
            )
            no_content = (
                (await session.execute(no_content_stmt)).scalars().all()
            )

            if no_content:
                content_result = await extract_contents(
                    session, list(no_content)
                )
                result.content_extracted = content_result.extracted_count
                result.content_skipped = content_result.skipped_count
                result.content_errors = content_result.error_count
            else:
                logger.info(
                    "scheduler_content_skipped",
                    reason="no articles without content",
                )

            # Phase 4: Query unanalyzed articles
            unanalyzed_stmt = (
                select(NewsArticle)
                .outerjoin(
                    AnalysisResult,
                    AnalysisResult.news_article_id == NewsArticle.id,
                )
                .where(AnalysisResult.id == None)  # noqa: E711
            )
            unanalyzed = (await session.execute(unanalyzed_stmt)).scalars().all()

            if not unanalyzed:
                logger.info(
                    "scheduler_analyze_skipped",
                    reason="no unanalyzed articles",
                )
            else:
                # Phase 5: Run AI analysis (with content if available)
                analyze_result = await analyze_articles(session, list(unanalyzed))
                result.analyze_count = analyze_result.analyzed_count
                result.analyze_skipped = analyze_result.skipped_count
                result.analyze_errors = analyze_result.error_count

    except Exception:
        logger.exception("scheduler_job_failed")
        return result

    global _last_fetch_at
    _last_fetch_at = datetime.now(timezone.utc)

    logger.info(
        "scheduler_job_completed",
        keywords=result.keywords_count,
        fetch_new=result.fetch_new,
        fetch_skipped=result.fetch_skipped,
        fetch_errors=result.fetch_errors,
        content_extracted=result.content_extracted,
        content_skipped=result.content_skipped,
        content_errors=result.content_errors,
        analyzed=result.analyze_count,
        analyze_skipped=result.analyze_skipped,
        analyze_errors=result.analyze_errors,
    )
    return result


def start_scheduler() -> None:
    """Start the APScheduler background scheduler."""
    global _scheduler

    _scheduler = AsyncIOScheduler()
    _scheduler.add_job(
        run_fetch_and_analyze,
        trigger="interval",
        hours=settings.fetch_interval_hours,
        id="fetch_and_analyze",
        name="Fetch news and run AI analysis",
        replace_existing=True,
        max_instances=1,
    )
    _scheduler.start()

    logger.info(
        "scheduler_started",
        interval_hours=settings.fetch_interval_hours,
    )


def stop_scheduler() -> None:
    """Stop the APScheduler background scheduler if running."""
    global _scheduler

    if _scheduler is not None and _scheduler.running:
        _scheduler.shutdown(wait=False)
        logger.info("scheduler_stopped")
        _scheduler = None
