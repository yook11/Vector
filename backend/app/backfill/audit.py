"""再投入の受付結果と実行失敗をbest-effortで記録する。"""

import structlog

from app.audit.domain.event import EventType
from app.audit.error_fields import exception_fqn
from app.audit.metrics import record_audit_dropped
from app.audit.stages.backfill import (
    BackfillAuditRepository,
    BackfillOutcomeCode,
    BackfillStage,
    BackfillTargetKind,
)
from app.backfill.targets import BackfillTarget
from app.db.session import SessionFactory

logger = structlog.get_logger(__name__)


async def append_backfill_item_event(
    session_factory: SessionFactory,
    *,
    backfill_stage: BackfillStage,
    run_id: str,
    target_kind: BackfillTargetKind,
    target: BackfillTarget,
    event_type: EventType,
    outcome_code: BackfillOutcomeCode,
    exc: BaseException | None = None,
) -> None:
    """item 単位監査を best-effort で焼く。"""
    stage = BackfillAuditRepository.stage_for(backfill_stage)
    try:
        async with session_factory() as session:
            await BackfillAuditRepository(session).append_item_event(
                event_type=event_type,
                outcome_code=outcome_code,
                backfill_stage=backfill_stage,
                run_id=run_id,
                target_kind=target_kind,
                target_id=target.target_id,
                analyzable_article_id=target.analyzable_article_id,
                source_name=target.source_name,
                exc=exc,
            )
            await session.commit()
    except Exception as audit_exc:  # noqa: BLE001
        try:
            logger.warning(
                "backfill_item_audit_dropped",
                stage=stage.value,
                backfill_stage=backfill_stage,
                outcome_code=outcome_code.value,
                audit_error_class=exception_fqn(audit_exc),
            )
        except Exception:  # noqa: S110
            # 診断の障害でも元の結果と後続処理を維持する。
            pass
        try:
            record_audit_dropped(stage)
        except Exception:  # noqa: S110
            # 計測の障害でも元の結果と後続処理を維持する。
            pass


async def append_backfill_run_event(
    session_factory: SessionFactory,
    *,
    backfill_stage: BackfillStage,
    run_id: str,
    event_type: EventType,
    outcome_code: BackfillOutcomeCode,
    daily_max: int | None = None,
    exc: BaseException | None = None,
) -> None:
    """run 単位の棄却 (予算枯渇 REJECTED) / 失敗監査を best-effort で焼く。"""
    stage = BackfillAuditRepository.stage_for(backfill_stage)
    try:
        async with session_factory() as session:
            await BackfillAuditRepository(session).append_run_event(
                event_type=event_type,
                outcome_code=outcome_code,
                backfill_stage=backfill_stage,
                run_id=run_id,
                daily_max=daily_max,
                exc=exc,
            )
            await session.commit()
    except Exception as audit_exc:  # noqa: BLE001
        try:
            logger.warning(
                "backfill_run_audit_dropped",
                stage=stage.value,
                backfill_stage=backfill_stage,
                outcome_code=outcome_code.value,
                audit_error_class=exception_fqn(audit_exc),
            )
        except Exception:  # noqa: S110
            # 診断の障害でも元の結果と後続処理を維持する。
            pass
        try:
            record_audit_dropped(stage)
        except Exception:  # noqa: S110
            # 計測の障害でも元の結果と後続処理を維持する。
            pass
