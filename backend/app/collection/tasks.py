"""収集タスク — パイプラインの前段。

経路: ``dispatch_sources`` → ``ingest_source`` → ``curation.tasks.curate_content``。
本文込みで取得できた記事は ``curate_content`` に直接 chain、本文未取得の記事は
``acquire_html_body`` task で HTML 取得 + 抽出 + 永続化へ進む。
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
)
from app.collection.source_fetch.failure_handling import SourceFetchFailureHandler
from app.collection.staged import IngestSourceArg
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
    """単一 FetchLog 行を書き込む。"""
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
# Dispatch
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
        await ingest_source.kiq(IngestSourceArg(id=source.id, name=str(source.name)))

    result = {"dispatched_count": len(sources)}
    logger.info("dispatch_sources_completed", **result)
    return result


# ---------------------------------------------------------------------------
# Ingest — per-source の取り込み
# ---------------------------------------------------------------------------


@broker_content.task(
    task_name="ingest_source",
    timeout=300,
    max_retries=0,
    retry_on_error=False,
)
async def ingest_source(
    arg: IngestSourceArg,
    ctx: Context = TaskiqDepends(),
) -> dict:
    """ソースを取り込む。

    ``arg.id`` は ``news_sources.id`` (FK 用)、``arg.name`` は Fetcher dispatch の
    lookup キー。本文込みで取れた記事は永続化して ``curate_content`` に enqueue、
    本文未取得の記事は後段 ``acquire_html_body`` task へ進む。

    失敗ハンドリング: taskiq inline retry を持たず (``max_retries=0``)、捕捉した
    例外は ``SourceFetchFailureHandler`` に委譲する。次の cron tick で再 dispatch
    される。
    """
    from app.analysis.curation.domain.ready import CurationTrigger
    from app.analysis.curation.tasks import curate_content
    from app.collection.source_fetch.service import ArticleAcquisitionService
    from app.collection.source_fetch.strategy import FETCHERS

    source_id = arg.id
    logger.info("ingest_source_started", source_id=source_id, source_name=arg.name)
    session_factory = ctx.state.session_factory
    start_time = time.monotonic()

    fetcher_factory = FETCHERS[arg.name]
    svc = ArticleAcquisitionService(session_factory, fetcher_factory)

    handler = SourceFetchFailureHandler(session_factory)
    try:
        persisted_ids = await svc.execute(source_id)
    except Exception as exc:
        # FetchLog (実行結果記録) は Task 層が書く。audit / reraise 判断は
        # handler に委譲する。
        await _record_fetch_log(
            session_factory, source_id, FetchStatus.ERROR, 0, str(exc), start_time
        )
        reraise = await handler.handle(
            source_id=source_id,
            source_name=arg.name,
            exc=exc,
            attempt=1,
        )
        if reraise:
            raise
        return {"source_id": source_id, "status": "error", "reason": str(exc)}

    article_created_count = len(persisted_ids)
    await _record_fetch_log(
        session_factory,
        source_id,
        FetchStatus.SUCCESS,
        article_created_count,
        None,
        start_time,
    )
    # 永続化済 article_id を Trigger に詰めて enqueue。
    for article_id in persisted_ids:
        await curate_content.kiq(CurationTrigger(article_id=article_id))
    # 本文未取得分は `incomplete_articles` の DB 駆動。`dispatch_html_fetch_jobs`
    # cron poller が `acquire_html_body` に投入するため、ここでは直接 kiq しない。
    payload = {
        "source_id": source_id,
        "source_name": arg.name,
        "status": "success",
        "article_created_count": article_created_count,
    }
    logger.info("ingest_source_completed", **payload)
    return payload


# ---------------------------------------------------------------------------
# 2 段目 — HTML 取得 + 本文抽出 + Article 永続化
# ---------------------------------------------------------------------------


@broker_content.task(
    task_name="acquire_html_body",
    timeout=60,
    max_retries=0,
    retry_on_error=False,
)
async def acquire_html_body(
    pending_id: int,
    ctx: Context = TaskiqDepends(),
) -> dict | None:
    """HTML 取得 + 本文抽出 + Article 永続化を Service に委譲。

    taskiq retry を持たず cron poller (``dispatch_html_fetch_jobs``) のみで
    再投入する。task は ``ReadyForArticleCompletion.try_advance_from`` で Ready を
    構築し (構築不能なら skip log + ``None``)、Service に渡す。article_id が返れば
    ``curate_content`` に enqueue、``None`` は何もしない (DB 状態 + audit は
    Service / failure handler 内で完結済)。
    """
    from app.analysis.curation.domain.ready import CurationTrigger
    from app.analysis.curation.tasks import curate_content
    from app.collection.article_completion.ready import ReadyForArticleCompletion
    from app.collection.article_completion.repository import (
        ArticleCompletionRepository,
    )
    from app.collection.article_completion.service import ArticleCompletionService

    session_factory = ctx.state.session_factory
    async with session_factory() as session:
        ready = await ReadyForArticleCompletion.try_advance_from(
            pending_id=pending_id,
            repo=ArticleCompletionRepository(session),
        )
    if ready is None:
        logger.info(
            "acquire_html_body_skipped",
            pending_id=pending_id,
            reason="precondition_not_met",
        )
        return None

    article_id = await ArticleCompletionService(session_factory).execute(ready)

    if article_id is None:
        return None
    await curate_content.kiq(CurationTrigger(article_id=article_id))
    return {
        "pending_id": pending_id,
        "article_id": article_id,
        "status": "success",
    }
