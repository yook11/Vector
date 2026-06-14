"""Backfill stage の監査イベントを組み立てる。"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.domain.event import EventType, Stage
from app.audit.domain.payloads import BackfillPayload
from app.audit.error_chain import extract_error_chain
from app.audit.error_fields import error_message_of, exception_fqn
from app.audit.failure_projection import Retryability
from app.audit.repository import PipelineEventRepository

BackfillStage = Literal["curate", "assess", "embed"]
BackfillTargetKind = Literal["article", "curation", "analyzed_article"]


class BackfillOutcomeCode(StrEnum):
    """Backfill stage の outcome code。"""

    ITEM_ENQUEUED = "backfill_item_enqueued"
    ITEM_ENQUEUE_FAILED = "backfill_item_enqueue_failed"
    RUN_NO_TARGETS = "backfill_run_no_targets"
    RUN_KILL_SWITCH_DISABLED = "backfill_run_kill_switch_disabled"
    RUN_HELD_BY_STAGE_HOLD = "backfill_run_held_by_stage_hold"
    RUN_DAILY_BUDGET_EXHAUSTED = "backfill_run_daily_budget_exhausted"
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
        analyzable_article_id: int | None,
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
            error_message=error_message_of(exc),
            error_chain=extract_error_chain(exc) if exc is not None else None,
        )
        await self._events.append(
            stage=stage,
            event_type=event_type,
            outcome_code=outcome_code.value,
            payload=payload,
            article_id=analyzable_article_id,
            error_class=exception_fqn(exc) if exc is not None else None,
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
        daily_max: int | None = None,
        exc: BaseException | None = None,
        retryability: Retryability | None = None,
    ) -> None:
        """run 単位の skip 制御 / 失敗を記録する (成功 summary は焼かない)。"""
        payload = BackfillPayload(
            backfill_stage=backfill_stage,
            run_id=run_id,
            daily_max=daily_max,
            error_message=error_message_of(exc),
            error_chain=extract_error_chain(exc) if exc is not None else None,
        )
        await self._events.append(
            stage=stage,
            event_type=event_type,
            outcome_code=outcome_code.value,
            payload=payload,
            error_class=exception_fqn(exc) if exc is not None else None,
            retryability=retryability,
        )
