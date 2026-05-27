"""収集 (acquisition) タスク — パイプラインの最前段 (Stage 1)。

経路: ``dispatch_high/medium/low`` (cron) または ``dispatch_sources`` (admin 手動) →
``acquire_source`` → ``curate_content`` chain (本文込み) または
``scrape_html_body`` (completion, DB 駆動)。本ファイルは Stage 1 の cron dispatch
と per-source 取り込みに絞り、HTML 取得 + 本文抽出 (Stage 2) は
``app/queue/tasks/completion.py`` の責務。

dispatch 系 task は ``SourceDispatchService`` に「何を dispatch すべきか」の決定を
委譲する。task の責務は VO リストを kiq message DTO に変換して ``.kiq()`` を
呼ぶ orchestration のみ。
"""

from __future__ import annotations

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from taskiq import Context, TaskiqDepends

from app.collection.article_acquisition.failure_handling import (
    ArticleAcquisitionFailureHandler,
)
from app.collection.sources.dispatch import SourceDispatchService
from app.collection.sources.fetch_cadence import FetchCadence
from app.queue.brokers import broker_content, broker_metadata
from app.queue.messages.collection import AcquireSourceArg
from app.queue.messages.curation import CurationTrigger
from app.queue.schedule import CADENCE_CRON
from app.queue.tasks.curation import curate_content

logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Dispatch — tier 別 cron + 全 tier 一括 (admin 手動)
# ---------------------------------------------------------------------------


async def _dispatch(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    cadence: FetchCadence | None,
) -> dict:
    """``SourceDispatchService`` で対象を決め、各 source に acquire を kiq する。

    Service は「何を dispatch するか」を VO で返し、本関数 (task orchestration)
    が VO を ``AcquireSourceArg`` に変換して ``.kiq()`` を呼ぶ。
    """
    service = SourceDispatchService(session_factory)
    targets = await service.select(cadence)
    for t in targets:
        await acquire_source.kiq(AcquireSourceArg(id=t.id, name=str(t.name)))
    result = {"dispatched_count": len(targets)}
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
    return await _dispatch(ctx.state.session_factory, cadence=FetchCadence.HIGH)


@broker_metadata.task(
    task_name="dispatch_medium",
    timeout=60,
    max_retries=1,
    retry_on_error=True,
    schedule=[{"cron": CADENCE_CRON[FetchCadence.MEDIUM]}],
)
async def dispatch_medium(ctx: Context = TaskiqDepends()) -> dict:
    """MEDIUM tier のソースを dispatch する (1 時間間隔)。"""
    return await _dispatch(ctx.state.session_factory, cadence=FetchCadence.MEDIUM)


@broker_metadata.task(
    task_name="dispatch_low",
    timeout=60,
    max_retries=1,
    retry_on_error=True,
    schedule=[{"cron": CADENCE_CRON[FetchCadence.LOW]}],
)
async def dispatch_low(ctx: Context = TaskiqDepends()) -> dict:
    """LOW tier のソースを dispatch する (6 時間間隔)。"""
    return await _dispatch(ctx.state.session_factory, cadence=FetchCadence.LOW)


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
    return await _dispatch(ctx.state.session_factory, cadence=None)


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
    例外は ``ArticleAcquisitionFailureHandler`` に委譲する。次の cron tick で再 dispatch
    される。
    """
    # 重い import は task body 内 (scheduler 起動を軽く保つ)。
    from app.collection.article_acquisition.service import ArticleAcquisitionService
    from app.collection.article_acquisition.strategy import SOURCES
    from app.collection.sources.source_name import SourceName

    source_id = arg.id
    logger.info("acquire_source_started", source_id=source_id, source_name=arg.name)
    session_factory = ctx.state.session_factory

    source = SOURCES[SourceName(arg.name)]
    svc = ArticleAcquisitionService(session_factory, source)

    handler = ArticleAcquisitionFailureHandler(session_factory)
    try:
        persisted_ids = await svc.execute(source_id)
    except Exception as exc:
        reraise = await handler.handle_source_failure(
            source_id=source_id,
            source_name=arg.name,
            exc=exc,
        )
        if reraise:
            raise
        return {"source_id": source_id, "status": "error", "reason": str(exc)}

    article_created_count = len(persisted_ids)
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
