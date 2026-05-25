"""収集タスク — パイプラインの前段。

経路: ``dispatch_sources`` → ``acquire_source`` → ``curation.tasks.curate_content``。
本文込みで取得できた記事は ``curate_content`` に直接 chain、本文未取得の記事は
``scrape_html_body`` task で HTML 取得 + 抽出 + 永続化へ進む。
"""

from __future__ import annotations

import time

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlmodel import select
from taskiq import Context, TaskiqDepends

from app.brokers import (
    CADENCE_CRON,
    broker_content,
    broker_metadata,
)
from app.collection.article_acquisition.failure_handling import (
    SourceAcquisitionFailureHandler,
)
from app.collection.sources.fetch_cadence import FetchCadence
from app.collection.staged import AcquireSourceArg
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
# Dispatch — tier 別 cron + 全 tier 一括 (admin 手動)
# ---------------------------------------------------------------------------


async def _dispatch_active_sources(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    cadence: FetchCadence | None,
) -> dict:
    """active なソースを走査し、各ソースに個別 acquire タスクを dispatch する。

    ``cadence`` 指定時はその tier の source 定義のみ dispatch する。``None`` は
    全 tier 一括 (admin 手動 fetch 経路)。DB の active source 名が ``SOURCES`` に
    無い場合はコード未登録としてスキップする (failure-visibility のため warning)。
    """
    # SOURCES は import が重いため lazy (scheduler の tasks.py import を軽く保つ)。
    from app.collection.article_acquisition.strategy import SOURCES
    from app.shared.value_objects.source_name import SourceName

    async with session_factory() as session:
        rows = list(
            (
                await session.execute(
                    select(NewsSource.id, NewsSource.name)
                    .where(NewsSource.is_active == True)  # noqa: E712
                    .order_by(NewsSource.name)
                )
            ).all()
        )

    dispatched = 0
    for row in rows:
        source_def = SOURCES.get(SourceName(row.name))
        if source_def is None:
            logger.warning("dispatch_source_unknown", source_name=str(row.name))
            continue
        if cadence is not None and source_def.fetch_cadence is not cadence:
            continue
        await acquire_source.kiq(AcquireSourceArg(id=row.id, name=str(row.name)))
        dispatched += 1

    result = {"dispatched_count": dispatched}
    logger.info(
        "dispatch_sources_completed",
        cadence=cadence.value if cadence is not None else "all",
        **result,
    )
    return result


@broker_metadata.task(
    task_name="dispatch_high",
    timeout=60,
    max_retries=1,
    retry_on_error=True,
    schedule=[{"cron": CADENCE_CRON[FetchCadence.HIGH]}],
)
async def dispatch_high(ctx: Context = TaskiqDepends()) -> dict:
    """HIGH tier のソースを dispatch する (15 分間隔)。"""
    return await _dispatch_active_sources(
        ctx.state.session_factory, cadence=FetchCadence.HIGH
    )


@broker_metadata.task(
    task_name="dispatch_medium",
    timeout=60,
    max_retries=1,
    retry_on_error=True,
    schedule=[{"cron": CADENCE_CRON[FetchCadence.MEDIUM]}],
)
async def dispatch_medium(ctx: Context = TaskiqDepends()) -> dict:
    """MEDIUM tier のソースを dispatch する (1 時間間隔)。"""
    return await _dispatch_active_sources(
        ctx.state.session_factory, cadence=FetchCadence.MEDIUM
    )


@broker_metadata.task(
    task_name="dispatch_low",
    timeout=60,
    max_retries=1,
    retry_on_error=True,
    schedule=[{"cron": CADENCE_CRON[FetchCadence.LOW]}],
)
async def dispatch_low(ctx: Context = TaskiqDepends()) -> dict:
    """LOW tier のソースを dispatch する (6 時間間隔)。"""
    return await _dispatch_active_sources(
        ctx.state.session_factory, cadence=FetchCadence.LOW
    )


@broker_metadata.task(
    task_name="dispatch_sources",
    timeout=60,
    max_retries=1,
    retry_on_error=True,
)
async def dispatch_sources(
    ctx: Context = TaskiqDepends(),
) -> dict:
    """全 tier の active ソースを一括 dispatch する (admin 手動 fetch 経路)。

    cron 発火は tier 別 ``dispatch_high`` / ``dispatch_medium`` / ``dispatch_low``
    が担うため、本タスクは schedule を持たず ``.kiq()`` 明示呼び出し専用。
    """
    logger.info("dispatch_sources_started")
    return await _dispatch_active_sources(ctx.state.session_factory, cadence=None)


# ---------------------------------------------------------------------------
# Ingest — per-source の取り込み
# ---------------------------------------------------------------------------


@broker_content.task(
    task_name="acquire_source",
    timeout=300,
    max_retries=0,
    retry_on_error=False,
)
async def acquire_source(
    arg: AcquireSourceArg,
    ctx: Context = TaskiqDepends(),
) -> dict:
    """ソースを取り込む。

    ``arg.id`` は ``news_sources.id`` (FK 用)、``arg.name`` は ``SOURCES`` dispatch の
    lookup キー。本文込みで取れた記事は永続化して ``curate_content`` に enqueue、
    本文未取得の記事は後段 ``scrape_html_body`` task へ進む。

    失敗ハンドリング: taskiq inline retry を持たず (``max_retries=0``)、捕捉した
    例外は ``SourceAcquisitionFailureHandler`` に委譲する。次の cron tick で再 dispatch
    される。
    """
    from app.analysis.curation.domain.ready import CurationTrigger
    from app.analysis.curation.tasks import curate_content
    from app.collection.article_acquisition.service import ArticleAcquisitionService
    from app.collection.article_acquisition.strategy import SOURCES
    from app.shared.value_objects.source_name import SourceName

    source_id = arg.id
    logger.info("acquire_source_started", source_id=source_id, source_name=arg.name)
    session_factory = ctx.state.session_factory
    start_time = time.monotonic()

    source = SOURCES[SourceName(arg.name)]
    svc = ArticleAcquisitionService(session_factory, source)

    handler = SourceAcquisitionFailureHandler(session_factory)
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
    # cron poller が `scrape_html_body` に投入するため、ここでは直接 kiq しない。
    payload = {
        "source_id": source_id,
        "source_name": arg.name,
        "status": "success",
        "article_created_count": article_created_count,
    }
    logger.info("acquire_source_completed", **payload)
    return payload


# ---------------------------------------------------------------------------
# 2 段目 — HTML 取得 + 本文抽出 + Article 永続化
# ---------------------------------------------------------------------------


@broker_content.task(
    task_name="scrape_html_body",
    timeout=60,
    max_retries=0,
    retry_on_error=False,
)
async def scrape_html_body(
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
            "scrape_html_body_skipped",
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
