"""旧Taskiq経路のbackfill定期実行と予算・hold制御。"""

from uuid import uuid4

import logfire
import structlog
from taskiq import Context, TaskiqDepends

from app.audit.domain.event import EventType, Stage
from app.audit.stages.backfill import BackfillOutcomeCode
from app.backfill.audit import (
    append_backfill_item_event as _append_backfill_item_event,
)
from app.backfill.audit import (
    append_backfill_run_event as _append_backfill_run_event,
)
from app.backfill.cleanup import (
    delete_aged_out_curations as _delete_aged_out_curations,
)
from app.backfill.cleanup import (
    exclude_aged_out_assessments as _exclude_aged_out_assessments,
)
from app.backfill.metrics import (
    backlog_gauge as _backlog_gauge,
)
from app.backfill.metrics import (
    record_aged_out as _record_aged_out,
)
from app.backfill.metrics import (
    record_dispatched as _record_dispatched,
)
from app.backfill.policy import (
    ASSESSMENTS_LIMIT,
    CURATIONS_LIMIT,
    BackfillWindow,
)
from app.config import settings
from app.logfire.stage_span import pipeline_stage_span
from app.queue.brokers import broker_maintenance
from app.queue.helpers.backlog import PipelineBacklog
from app.queue.helpers.budget import consume_daily_budget
from app.queue.helpers.stage_hold import is_stage_held
from app.queue.messages.assessment import AssessmentTrigger
from app.queue.messages.curation import CurationTrigger
from app.queue.schedule import (
    CRON_BACKFILL_ASSESSMENTS,
    CRON_BACKFILL_CURATIONS,
)
from app.queue.tasks.assessment import assess_content
from app.queue.tasks.curation import curate_content
from app.shared.time import utc_now

logger = structlog.get_logger(__name__)

CURATIONS_DAILY_MAX = 600
ASSESSMENTS_DAILY_MAX = 600

_held_gauge = logfire.metric_gauge(
    "vector.backfill.held",
    unit="1",
    description="backfill cron の最後の tick 時点で stage hold 中なら 1、通常なら 0",
)


def _new_backfill_run_id() -> str:
    """一回の実行に属する監査を識別する。"""
    return str(uuid4())


def _record_hold_state(stage: str, *, held: bool) -> None:
    """旧経路の停止状態を記録する。"""
    _held_gauge.set(1 if held else 0, attributes={"stage": stage})


@broker_maintenance.task(
    task_name="backfill_curations",
    timeout=120,
    max_retries=0,
    retry_on_error=False,
    schedule=[{"cron": CRON_BACKFILL_CURATIONS}],
)
async def backfill_curations(ctx: Context = TaskiqDepends()) -> None:
    """curation 子が NULL の Article を救済する (再投入 + 年齢削除)。

    救済機構の本体。3 段階で動く:

    1. **hold gate**: terminal_keep (key/残高/config 等の provider/stage 健全性
       問題) が起きると失敗ハンドラが ``curation:hold`` を立てる。hold 中は
       confirmed に失敗する AI 呼び出しを避けるため run 全体を skip する。
    2. **年齢削除**: 通常窓 (``[after, before)``) から落ちた 7 日超の未処理記事は
       「分析価値なし」として監査を焼いてから物理削除する。
    3. **通常再投入**: 窓内の child-NULL 記事を ID-only な ``CurationTrigger`` で
       kiq する。precondition 判定 / Ready 構築は下流 Stage 3 task に委ねる
       (Ready build blocked audit で観測可能)。
    """
    with pipeline_stage_span(Stage.BACKFILL_CURATE, op="backfill_curations"):
        session_factory = ctx.state.session_factory
        run_id = _new_backfill_run_id()
        if not settings.backfill_curations_enabled:
            # kill switch off = 運用ゲート。監査に焼かず log で観測する。
            logger.info("backfill_curations_disabled")
            return

        try:
            curation_held = await is_stage_held(
                ctx.state.pipeline_control_redis, Stage.CURATION
            )
            _record_hold_state("curation", held=curation_held)
            if curation_held:
                # stage hold = 運用ゲート。監査に焼かず log + held gauge で観測する。
                logger.warning("backfill_curations_held")
                return

            before, after = BackfillWindow().boundaries_at(utc_now())

            aged_out_count = await _delete_aged_out_curations(
                session_factory, created_before=after
            )
            _record_aged_out("curation", action="deleted", count=aged_out_count)

            async with session_factory() as session:
                backlog = PipelineBacklog(session)
                backlog_count = await backlog.count_articles_pending_curation(
                    created_before=before,
                    created_after=after,
                )
                targets = await backlog.curation_targets_pending(
                    created_before=before,
                    created_after=after,
                    limit=CURATIONS_LIMIT,
                )

            _backlog_gauge.set(backlog_count, attributes={"stage": "curation"})

            found = len(targets)
            if found == 0:
                # 対象 0 件 = 運用ゲート。監査に焼かず log + backlog gauge で観測する。
                logger.info("backfill_curations_empty")
                return

            granted = await consume_daily_budget(
                ctx.state.pipeline_control_redis, "curate", found, CURATIONS_DAILY_MAX
            )
            if granted == 0:
                # 予算上限に到達し実対象を先送り = run レベルの棄却。
                # benign skip ではなく REJECTED で監査に残す。
                await _append_backfill_run_event(
                    session_factory,
                    backfill_stage="curate",
                    run_id=run_id,
                    event_type=EventType.REJECTED,
                    outcome_code=BackfillOutcomeCode.RUN_DAILY_BUDGET_EXHAUSTED,
                    daily_max=CURATIONS_DAILY_MAX,
                )
                logger.warning("backfill_curations_daily_budget_exhausted", found=found)
                return

            enqueued = 0
            failed = 0
            for target in targets[:granted]:
                try:
                    await curate_content.kiq(
                        CurationTrigger(analyzable_article_id=target.target_id)
                    )
                except Exception as exc:  # noqa: BLE001
                    failed += 1
                    await _append_backfill_item_event(
                        session_factory,
                        backfill_stage="curate",
                        run_id=run_id,
                        target_kind="article",
                        target=target,
                        event_type=EventType.FAILED,
                        outcome_code=BackfillOutcomeCode.ITEM_ENQUEUE_FAILED,
                        exc=exc,
                    )
                    logger.warning(
                        "backfill_curations_kiq_failed",
                        analyzable_article_id=target.target_id,
                        error=str(exc),
                    )
                    continue

                enqueued += 1
                await _append_backfill_item_event(
                    session_factory,
                    backfill_stage="curate",
                    run_id=run_id,
                    target_kind="article",
                    target=target,
                    event_type=EventType.SUCCEEDED,
                    outcome_code=BackfillOutcomeCode.ITEM_ENQUEUED,
                )

            _record_dispatched("curation", enqueued)
        except Exception as exc:
            await _append_backfill_run_event(
                session_factory,
                backfill_stage="curate",
                run_id=run_id,
                event_type=EventType.FAILED,
                outcome_code=BackfillOutcomeCode.RUN_FAILED,
                exc=exc,
            )
            raise

        # article 既消滅 / 既処理 / 本文 oversized は、下流 Stage 3 task の
        # Ready build blocked audit で観測する
        logger.info(
            "backfill_curations_completed",
            found=found,
            granted=granted,
            requeued=enqueued,
        )


# ---------------------------------------------------------------------------
# Stage 2b: assessment の塩漬け救済
# ---------------------------------------------------------------------------


@broker_maintenance.task(
    task_name="backfill_assessments",
    timeout=120,
    max_retries=0,
    retry_on_error=False,
    schedule=[{"cron": CRON_BACKFILL_ASSESSMENTS}],
)
async def backfill_assessments(ctx: Context = TaskiqDepends()) -> None:
    """in-scope / out-of-scope assessment が無い Extraction を発見して
    assess_content を再投入する。

    maintenance は投入対象を見つけ、precondition 検証と Ready 構築は下流 task に
    委ねる。通常窓から落ちた未 assessment curation は削除せず exclusion を作る。
    """
    with pipeline_stage_span(Stage.BACKFILL_ASSESS, op="backfill_assessments"):
        session_factory = ctx.state.session_factory
        run_id = _new_backfill_run_id()
        if not settings.backfill_assessments_enabled:
            # kill switch off = 運用ゲート。監査に焼かず log で観測する。
            logger.info("backfill_assessments_disabled")
            return

        try:
            assessment_held = await is_stage_held(
                ctx.state.pipeline_control_redis, Stage.ASSESSMENT
            )
            _record_hold_state("assessment", held=assessment_held)
            if assessment_held:
                # stage hold = 運用ゲート。監査に焼かず log + held gauge で観測する。
                logger.warning("backfill_assessments_held")
                return

            before, after = BackfillWindow().boundaries_at(utc_now())

            aged_out_count = await _exclude_aged_out_assessments(
                session_factory, created_before=after
            )
            _record_aged_out("assessment", action="excluded", count=aged_out_count)

            async with session_factory() as session:
                backlog = PipelineBacklog(session)
                # 観測 (COUNT) → dispatch (target 取得) の順で同一 session 内に並べ、
                # read committed snapshot 上で一貫値を返す。
                backlog_count = await backlog.count_curations_pending_assessment(
                    created_before=before,
                    created_after=after,
                )
                targets = await backlog.assessment_targets_pending(
                    created_before=before,
                    created_after=after,
                    limit=ASSESSMENTS_LIMIT,
                )

            _backlog_gauge.set(backlog_count, attributes={"stage": "assessment"})

            found = len(targets)
            if found == 0:
                # 対象 0 件 = 運用ゲート。監査に焼かず log + backlog gauge で観測する。
                logger.info("backfill_assessments_empty")
                return

            granted = await consume_daily_budget(
                ctx.state.pipeline_control_redis, "assess", found, ASSESSMENTS_DAILY_MAX
            )
            if granted == 0:
                # 予算上限に到達し実対象を先送り = run レベルの棄却。
                # benign skip ではなく REJECTED で監査に残す。
                await _append_backfill_run_event(
                    session_factory,
                    backfill_stage="assess",
                    run_id=run_id,
                    event_type=EventType.REJECTED,
                    outcome_code=BackfillOutcomeCode.RUN_DAILY_BUDGET_EXHAUSTED,
                    daily_max=ASSESSMENTS_DAILY_MAX,
                )
                logger.warning(
                    "backfill_assessments_daily_budget_exhausted", found=found
                )
                return

            # ID のみ enqueue し、precondition 検証は Stage 4 task に委ねる。
            enqueued = 0
            failed = 0
            for target in targets[:granted]:
                try:
                    await assess_content.kiq(
                        AssessmentTrigger(curation_id=target.target_id),
                    )
                except Exception as exc:  # noqa: BLE001
                    failed += 1
                    await _append_backfill_item_event(
                        session_factory,
                        backfill_stage="assess",
                        run_id=run_id,
                        target_kind="curation",
                        target=target,
                        event_type=EventType.FAILED,
                        outcome_code=BackfillOutcomeCode.ITEM_ENQUEUE_FAILED,
                        exc=exc,
                    )
                    logger.warning(
                        "backfill_assessments_kiq_failed",
                        curation_id=target.target_id,
                        error=str(exc),
                    )
                    continue

                enqueued += 1
                await _append_backfill_item_event(
                    session_factory,
                    backfill_stage="assess",
                    run_id=run_id,
                    target_kind="curation",
                    target=target,
                    event_type=EventType.SUCCEEDED,
                    outcome_code=BackfillOutcomeCode.ITEM_ENQUEUED,
                )

            _record_dispatched("assessment", enqueued)
        except Exception as exc:
            await _append_backfill_run_event(
                session_factory,
                backfill_stage="assess",
                run_id=run_id,
                event_type=EventType.FAILED,
                outcome_code=BackfillOutcomeCode.RUN_FAILED,
                exc=exc,
            )
            raise

        logger.info(
            "backfill_assessments_completed",
            found=found,
            granted=granted,
            requeued=enqueued,
        )
