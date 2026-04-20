"""収集タスク — パイプラインの前段。

dispatch_sources → fetch_source_metadata → fetch_content
fetch_content 完了後、analysis.tasks.extract_content へチェーン。
"""

from __future__ import annotations

import time

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlmodel import select
from taskiq import Context, TaskiqDepends

from app.brokers import (
    _FETCH_CRON,
    broker_content,
    broker_metadata,
    is_last_attempt,
)
from app.collection.errors import PermanentFetchError, TemporaryFetchError
from app.collection.extraction.extractor import ArticleHtmlExtractor
from app.collection.extraction.service import ContentFetchService
from app.collection.ingestion.service import SourceFetchService
from app.models.fetch_log import FetchLog, FetchStatus
from app.models.news_source import NewsSource

logger = structlog.get_logger(__name__)


async def _record_fetch_log(
    session_factory: async_sessionmaker[AsyncSession],
    source_id: int,
    status: FetchStatus,
    articles_count: int,
    error_message: str | None,
    start_time: float,
) -> None:
    """単一 FetchLog 行を書き込む。Task 層の「実行結果記録」責務。"""
    duration_ms = int((time.monotonic() - start_time) * 1000)
    async with session_factory() as session:
        session.add(
            FetchLog(
                source_id=source_id,
                status=status,
                articles_count=articles_count,
                error_message=error_message,
                duration_ms=duration_ms,
            )
        )
        await session.commit()


# ---------------------------------------------------------------------------
# Metadata fetch — dispatch
# ---------------------------------------------------------------------------


@broker_metadata.task(
    task_name="dispatch_sources",
    timeout=60,
    max_retries=1,
    retry_on_error=True,
    schedule=[{"cron": _FETCH_CRON}],
)
async def dispatch_sources(
    ctx: Context = TaskiqDepends(),
) -> dict:
    """全アクティブソースを走査し、ソースごとに個別タスクを dispatch する。"""
    logger.info("dispatch_sources_started")
    session_factory = ctx.state.session_factory

    async with session_factory() as session:
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
        logger.info("dispatch_sources_skipped", reason="no active sources")
        return {"dispatched_count": 0}

    for source in sources:
        await fetch_source_metadata.kiq(source.id)

    result = {"dispatched_count": len(sources)}
    logger.info("dispatch_sources_completed", **result)
    return result


# ---------------------------------------------------------------------------
# Metadata fetch — per source
# ---------------------------------------------------------------------------


@broker_metadata.task(
    task_name="fetch_source_metadata",
    timeout=300,
    max_retries=2,
    retry_on_error=True,
)
async def fetch_source_metadata(
    source_id: int,
    ctx: Context = TaskiqDepends(),
) -> dict:
    """単一ソースのメタデータを取得し、新規記事を下流キューへ dispatch する。"""
    logger.info("fetch_source_metadata_started", source_id=source_id)
    session_factory = ctx.state.session_factory
    svc = SourceFetchService(session_factory)
    start_time = time.monotonic()

    try:
        result = await svc.execute(source_id)
    except PermanentFetchError as e:
        await _record_fetch_log(
            session_factory, source_id, FetchStatus.ERROR, 0, str(e), start_time
        )
        return {"source_id": source_id, "status": "error", "reason": str(e)}
    except TemporaryFetchError as e:
        await _record_fetch_log(
            session_factory, source_id, FetchStatus.ERROR, 0, str(e), start_time
        )
        if is_last_attempt(ctx):
            logger.warning(
                "fetch_source_metadata_max_retries",
                source_id=source_id,
                error=str(e),
            )
            return {"source_id": source_id, "status": "error", "reason": str(e)}
        raise

    if result.status == "not_found":
        return {"source_id": source_id, "status": "not_found"}
    if result.status == "skipped_quota":
        return {"source_id": source_id, "status": "skipped", "reason": "daily_quota"}

    new_count = len(result.new_discovered)
    await _record_fetch_log(
        session_factory, source_id, FetchStatus.SUCCESS, new_count, None, start_time
    )

    for discovered in result.new_discovered:
        await fetch_content.kiq(discovered.id)

    payload = {"source_id": source_id, "new_count": new_count, "status": "success"}
    logger.info("fetch_source_metadata_completed", **payload)
    return payload


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
    discovered_article_id: int,
    ctx: Context = TaskiqDepends(),
) -> None:
    """単一記事の本文コンテンツを取得し Article 行を作成する。"""
    from app.analysis.tasks import extract_content

    session_factory = ctx.state.session_factory
    html_extractor = ArticleHtmlExtractor()
    svc = ContentFetchService(session_factory, html_extractor)

    try:
        result = await svc.execute(discovered_article_id)
    except TemporaryFetchError:
        if is_last_attempt(ctx):
            logger.warning(
                "fetch_content_max_retries",
                discovered_article_id=discovered_article_id,
            )
            return
        raise

    # Article が作成された場合のみ分析にチェーン
    if result.status in ("fetched", "already_exists"):
        await extract_content.kiq(result.article_id)
