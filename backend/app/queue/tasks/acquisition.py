"""収集 (acquisition) タスク — パイプラインの最前段 (Stage 1)。

経路: ``dispatch_high/medium/low`` (cron) または ``dispatch_sources`` (admin 手動) →
``acquire_source`` → ``curate_content`` chain (本文込み) または
``scrape_html_body`` (completion, DB 駆動)。本ファイルは Stage 1 の cron dispatch
と per-source 取り込みに絞り、HTML 取得 + 本文抽出 (Stage 2) は
``app/queue/tasks/completion.py`` の責務。

dispatch 系 task は ``SourceDispatchService`` に「何を dispatch すべきか」の決定を
委譲する。task の責務は selection result を kiq message DTO に変換して
``.kiq()`` を呼び、source 単位の dispatch audit を焼く orchestration のみ。
"""

from __future__ import annotations

from enum import StrEnum

import logfire
import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from taskiq import Context, TaskiqDepends

from app.audit.domain.event import EventType
from app.audit.error_fields import exception_fqn
from app.audit.stages.dispatch import (
    DispatchAuditRepository,
    DispatchCadence,
    DispatchOutcomeCode,
)
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
# Logfire metrics (dispatch throughput / liveness)
# ---------------------------------------------------------------------------
#
# per-source 成功 occurrence と run 件数 summary は監査でなく metric の責務
# (rate / 成功率 / 生死)。attribute は closed 語彙のみで low-cardinality に保ち、
# source_id / source_name / 例外詳細は監査 (pipeline_events) に寄せる。

_dispatch_outcome_counter = logfire.metric_counter(
    "vector.dispatch.outcome",
    unit="1",
    description="dispatch per-source 結果件数 (cadence/result/reason 別の throughput)",
)
_dispatch_run_counter = logfire.metric_counter(
    "vector.dispatch.run",
    unit="1",
    description="dispatch run の結末 (cadence/outcome 別の liveness)",
)


class DispatchRunOutcome(StrEnum):
    """``vector.dispatch.run`` の outcome 属性 (run 全体の結末)。

    棄却 (rejected) は target 母集団外なので outcome には混ぜない (config 品質の信号は
    ``vector.dispatch.outcome{result=rejected}`` に残す)。全 active source が棄却された
    run は targets 空となり ``NO_TARGETS`` に畳む。
    """

    TARGET_SELECTION_FAILED = "target_selection_failed"
    NO_TARGETS = "no_targets"
    SUCCEEDED = "succeeded"
    PARTIAL_FAILED = "partial_failed"
    ALL_FAILED = "all_failed"


def _dispatch_run_outcome(
    *, target_count: int, dispatched_count: int
) -> DispatchRunOutcome:
    """対象あり run を enqueue 成功比で分類する (``target_count >= 1`` 前提)。"""
    if dispatched_count == target_count:
        return DispatchRunOutcome.SUCCEEDED
    if dispatched_count == 0:
        return DispatchRunOutcome.ALL_FAILED
    return DispatchRunOutcome.PARTIAL_FAILED


def _record_dispatch_run(
    cadence: DispatchCadence, *, outcome: DispatchRunOutcome
) -> None:
    """run の結末を low-cardinality counter に +1 する。"""
    _dispatch_run_counter.add(
        1, attributes={"cadence": cadence, "outcome": outcome.value}
    )


def _record_dispatch_outcome(
    cadence: DispatchCadence, *, result: str, reason: str, count: int
) -> None:
    """per-source 結果件数を closed attribute で加算する (0 は弾く)。"""
    if count:
        _dispatch_outcome_counter.add(
            count,
            attributes={"cadence": cadence, "result": result, "reason": reason},
        )


# ---------------------------------------------------------------------------
# Dispatch — tier 別 cron + 全 tier 一括 (admin 手動)
# ---------------------------------------------------------------------------


async def _dispatch(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    cadence: FetchCadence | None,
) -> dict:
    """``SourceDispatchService`` で対象を決め、各 source に acquire を kiq する。

    Service は「何を dispatch するか」と source 単位 rejection を返し、本関数
    (task orchestration) が target を ``AcquireSourceArg`` に変換して ``.kiq()``
    を呼ぶ。
    """
    cadence_value = _dispatch_cadence(cadence)
    service = SourceDispatchService(session_factory)
    try:
        selection = await service.select(cadence)
    except Exception as exc:
        await _append_dispatch_run_failed(
            session_factory,
            cadence=cadence_value,
            exc=exc,
        )
        _record_dispatch_run(
            cadence_value, outcome=DispatchRunOutcome.TARGET_SELECTION_FAILED
        )
        raise

    # rejection は which source + reason を監査に残しつつ reason 別に metric へ集計。
    rejection_counts: dict[str, int] = {}
    for rejection in selection.rejections:
        await _append_dispatch_source_event(
            session_factory,
            event_type=EventType.REJECTED,
            outcome_code=DispatchOutcomeCode(rejection.outcome_code.value),
            cadence=cadence_value,
            source_id=rejection.source_id,
            source_name=rejection.source_name,
            raw_source_name=rejection.raw_source_name,
            exc=rejection.exc,
        )
        reason = rejection.outcome_code.value
        rejection_counts[reason] = rejection_counts.get(reason, 0) + 1
    for reason, count in rejection_counts.items():
        _record_dispatch_outcome(
            cadence_value, result="rejected", reason=reason, count=count
        )

    if not selection.targets:
        # 0 件 (全棄却含む) は監査でなく metric で表す (死亡と idle を区別)。
        _record_dispatch_run(cadence_value, outcome=DispatchRunOutcome.NO_TARGETS)
        result = {"dispatched_count": 0}
        logger.info(
            "dispatch_sources_completed",
            cadence=cadence_value,
            selected_count=0,
            rejected_count=len(selection.rejections),
            failed_count=0,
            **result,
        )
        return result

    dispatched_count = 0
    failed_count = 0
    for t in selection.targets:
        try:
            await acquire_source.kiq(AcquireSourceArg(id=t.id, name=str(t.name)))
        except Exception as exc:  # noqa: BLE001
            failed_count += 1
            await _append_dispatch_source_event(
                session_factory,
                event_type=EventType.FAILED,
                outcome_code=DispatchOutcomeCode.SOURCE_ENQUEUE_FAILED,
                cadence=cadence_value,
                source_id=t.id,
                source_name=str(t.name),
                exc=exc,
            )
            logger.warning(
                "dispatch_source_enqueue_failed",
                source_id=t.id,
                source_name=str(t.name),
                cadence=cadence_value,
                error=str(exc),
            )
            continue

        dispatched_count += 1

    # admin lastSucceededAt[dispatch] を維持する最小 heartbeat (偽陽性回避に >=1 件)。
    if dispatched_count >= 1:
        await _append_dispatch_run_completed(session_factory, cadence=cadence_value)

    # per-source occurrence は監査から撤去し metric へ移設 (audit try/except の外)。
    _record_dispatch_outcome(
        cadence_value, result="dispatched", reason="none", count=dispatched_count
    )
    _record_dispatch_outcome(
        cadence_value,
        result="enqueue_failed",
        reason="unclassified",
        count=failed_count,
    )
    _record_dispatch_run(
        cadence_value,
        outcome=_dispatch_run_outcome(
            target_count=len(selection.targets), dispatched_count=dispatched_count
        ),
    )

    result = {"dispatched_count": dispatched_count}
    logger.info(
        "dispatch_sources_completed",
        cadence=cadence_value,
        selected_count=len(selection.targets),
        rejected_count=len(selection.rejections),
        failed_count=failed_count,
        **result,
    )
    return result


def _dispatch_cadence(cadence: FetchCadence | None) -> DispatchCadence:
    """監査 payload に保存する dispatch cadence wire 値。"""
    return cadence.value if cadence is not None else "all"


async def _append_dispatch_source_event(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    event_type: EventType,
    outcome_code: DispatchOutcomeCode,
    cadence: DispatchCadence,
    source_id: int | None,
    source_name: str | None,
    raw_source_name: str | None = None,
    exc: BaseException | None = None,
) -> None:
    """source 単位監査を best-effort で焼く。"""
    try:
        async with session_factory() as session:
            await DispatchAuditRepository(session).append_source_event(
                event_type=event_type,
                outcome_code=outcome_code,
                cadence=cadence,
                source_id=source_id,
                source_name=source_name,
                raw_source_name=raw_source_name,
                exc=exc,
            )
            await session.commit()
    except Exception as audit_exc:
        logger.exception(
            "dispatch_source_audit_dropped",
            outcome_code=outcome_code.value,
            source_id=source_id,
            source_name=source_name,
            audit_error_class=(exception_fqn(audit_exc)),
        )


async def _append_dispatch_run_completed(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    cadence: DispatchCadence,
) -> None:
    """run 成功 heartbeat (件数なし) を best-effort で焼く。"""
    try:
        async with session_factory() as session:
            await DispatchAuditRepository(session).append_run_completed(cadence=cadence)
            await session.commit()
    except Exception as audit_exc:
        logger.exception(
            "dispatch_run_audit_dropped",
            outcome_code=DispatchOutcomeCode.DISPATCH_RUN_COMPLETED.value,
            audit_error_class=(exception_fqn(audit_exc)),
        )


async def _append_dispatch_run_event(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    event_type: EventType,
    outcome_code: DispatchOutcomeCode,
    cadence: DispatchCadence,
    exc: BaseException | None = None,
) -> None:
    """run 単位の失敗監査を best-effort で焼く。"""
    try:
        async with session_factory() as session:
            await DispatchAuditRepository(session).append_run_event(
                event_type=event_type,
                outcome_code=outcome_code,
                cadence=cadence,
                exc=exc,
            )
            await session.commit()
    except Exception as audit_exc:
        logger.exception(
            "dispatch_run_audit_dropped",
            outcome_code=outcome_code.value,
            audit_error_class=(exception_fqn(audit_exc)),
        )


async def _append_dispatch_run_failed(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    cadence: DispatchCadence,
    exc: BaseException,
) -> None:
    """selection 自体が成立しない run failure を監査する。"""
    await _append_dispatch_run_event(
        session_factory,
        event_type=EventType.FAILED,
        outcome_code=DispatchOutcomeCode.DISPATCH_RUN_FAILED,
        cadence=cadence,
        exc=exc,
    )


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
