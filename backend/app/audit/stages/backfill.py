"""Backfill stage の監査イベントを組み立てる。"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.domain.event import EventType, Stage
from app.audit.domain.payloads import BackfillPayload
from app.audit.error_chain import extract_error_chain
from app.audit.failure_projection import Retryability
from app.audit.repository import PipelineEventRepository
from app.shared.security.redaction import redact_secrets

BackfillStage = Literal["curate", "assess", "embed"]
BackfillTargetKind = Literal["article", "curation", "analysis"]

_ERROR_MESSAGE_LIMIT = 2000


class BackfillOutcomeCode(StrEnum):
    """Backfill stage の outcome code。"""

    ITEM_ENQUEUED = "backfill_item_enqueued"
    ITEM_ENQUEUE_FAILED = "backfill_item_enqueue_failed"
    RUN_NO_TARGETS = "backfill_run_no_targets"
    RUN_KILL_SWITCH_DISABLED = "backfill_run_kill_switch_disabled"
    RUN_HELD_BY_STAGE_HOLD = "backfill_run_held_by_stage_hold"
    RUN_DAILY_BUDGET_EXHAUSTED = "backfill_run_daily_budget_exhausted"
    RUN_COMPLETED = "backfill_run_completed"
    RUN_FAILED = "backfill_run_failed"


class BackfillAuditRepository:
    """Backfill 専用の payload / outcome_code を決める。"""

    def __init__(self, session: AsyncSession) -> None:
        self._events = PipelineEventRepository(session)

    async def append_item_event(
        self,
        *,
        stage: Stage,
        event_type: EventType,
        outcome_code: BackfillOutcomeCode,
        backfill_stage: BackfillStage,
        run_id: str,
        target_kind: BackfillTargetKind,
        target_id: int,
        article_id: int | None,
        source_name: str | None,
        exc: BaseException | None = None,
        retryability: Retryability | None = None,
    ) -> None:
        """item 単位の backfill enqueue 結果を記録する。"""
        payload = BackfillPayload(
            backfill_stage=backfill_stage,
            run_id=run_id,
            target_kind=target_kind,
            target_id=target_id,
            source_name=source_name,
            error_message=_error_message(exc),
            error_chain=extract_error_chain(exc) if exc is not None else None,
        )
        await self._events.append(
            stage=stage,
            event_type=event_type,
            outcome_code=outcome_code.value,
            payload=payload,
            article_id=article_id,
            error_class=_fqn(exc) if exc is not None else None,
            retryability=retryability,
        )

    async def append_run_event(
        self,
        *,
        stage: Stage,
        event_type: EventType,
        outcome_code: BackfillOutcomeCode,
        backfill_stage: BackfillStage,
        run_id: str,
        selected_count: int | None = None,
        granted_count: int | None = None,
        enqueued_count: int | None = None,
        failed_count: int | None = None,
        limit: int | None = None,
        daily_max: int | None = None,
        exc: BaseException | None = None,
        retryability: Retryability | None = None,
    ) -> None:
        """run 単位の backfill 結果を記録する。"""
        payload = BackfillPayload(
            backfill_stage=backfill_stage,
            run_id=run_id,
            selected_count=selected_count,
            granted_count=granted_count,
            enqueued_count=enqueued_count,
            failed_count=failed_count,
            limit=limit,
            daily_max=daily_max,
            error_message=_error_message(exc),
            error_chain=extract_error_chain(exc) if exc is not None else None,
        )
        await self._events.append(
            stage=stage,
            event_type=event_type,
            outcome_code=outcome_code.value,
            payload=payload,
            error_class=_fqn(exc) if exc is not None else None,
            retryability=retryability,
        )


def _error_message(exc: BaseException | None) -> str | None:
    if exc is None:
        return None
    return redact_secrets(str(exc))[:_ERROR_MESSAGE_LIMIT] or None


def _fqn(exc: BaseException) -> str:
    return f"{type(exc).__module__}.{type(exc).__qualname__}"
