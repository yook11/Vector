"""収集タスク — パイプラインの前段。

dispatch_sources → fetch_source_metadata → fetch_content
fetch_content 完了後、analysis.tasks.extract_content へチェーン。
"""

from __future__ import annotations

import time

import httpx
import structlog
from sqlmodel import select
from taskiq import Context, TaskiqDepends

from app.brokers import (
    _FETCH_CRON,
    broker_content,
    broker_metadata,
    is_last_attempt,
)
from app.collection.extraction.extractor import (
    ArticleHtmlExtractor,
    TemporaryFetchError,
)
from app.collection.extraction.service import ContentFetchService
from app.collection.ingestion.quota import check_daily_quota
from app.collection.ingestion.registry import get_fetcher
from app.models.fetch_log import FetchLog, FetchStatus
from app.models.news_source import NewsSource

logger = structlog.get_logger(__name__)

_USER_AGENT = "Mozilla/5.0 (compatible; Vector/1.0; +https://github.com/yook11/Vector)"

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

    async with session_factory() as session:
        source = await session.get(NewsSource, source_id)
        if source is None:
            logger.warning(
                "fetch_source_metadata_skipped",
                source_id=source_id,
                reason="source not found",
            )
            return {"source_id": source_id, "status": "not_found"}

        fetcher = get_fetcher(source)

        daily_limit = getattr(fetcher, "DAILY_REQUEST_LIMIT", None)
        if daily_limit is not None:
            if not await check_daily_quota(source.id, daily_limit):
                logger.info(
                    "fetch_source_metadata_quota_exceeded",
                    source_id=source_id,
                    source=source.name,
                )
                return {
                    "source_id": source_id,
                    "status": "skipped",
                    "reason": "daily_quota",
                }

        start_time = time.monotonic()
        async with httpx.AsyncClient(headers={"User-Agent": _USER_AGENT}) as client:
            source_result = await fetcher.fetch(client, session, source)
        duration_ms = int((time.monotonic() - start_time) * 1000)

        # FetchLog を記録
        fetch_log = FetchLog(
            source_id=source.id,
            status=(
                FetchStatus.SUCCESS if source_result.success else FetchStatus.ERROR
            ),
            articles_count=source_result.new_count,
            error_message=source_result.error_message,
            duration_ms=duration_ms,
        )
        session.add(fetch_log)
        await session.commit()

    # 全件 fetch_content へ dispatch
    for discovered in source_result.new_discovered:
        await fetch_content.kiq(discovered.id)

    result = {
        "source_id": source_id,
        "new_count": source_result.new_count,
        "skipped_count": source_result.skipped_count,
        "success": source_result.success,
    }
    logger.info("fetch_source_metadata_completed", **result)
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
