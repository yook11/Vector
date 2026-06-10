"""Dispatch stage の監査イベントを組み立てる。"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.domain.event import EventType, Stage
from app.audit.domain.payloads import DispatchPayload
from app.audit.error_chain import extract_error_chain
from app.audit.error_fields import error_message_of, exception_fqn
from app.audit.failure_projection import Retryability
from app.audit.repository import PipelineEventRepository
from app.shared.security.redaction import redact_secrets

DispatchCadence = Literal["high", "medium", "low", "all"]

_RAW_SOURCE_NAME_LIMIT = 200


class DispatchOutcomeCode(StrEnum):
    """Stage.DISPATCH の outcome code。"""

    SOURCE_ENQUEUE_FAILED = "source_enqueue_failed"
    SOURCE_NOT_REGISTERED = "source_not_registered"
    SOURCE_NAME_INVALID = "source_name_invalid"
    DISPATCH_RUN_COMPLETED = "dispatch_run_completed"
    DISPATCH_RUN_FAILED = "dispatch_run_failed"


class DispatchAuditRepository:
    """Dispatch 専用の payload / outcome_code を決める。"""

    def __init__(self, session: AsyncSession) -> None:
        self._events = PipelineEventRepository(session)

    async def append_source_event(
        self,
        *,
        event_type: EventType,
        outcome_code: DispatchOutcomeCode,
        cadence: DispatchCadence,
        source_id: int | None,
        source_name: str | None,
        raw_source_name: str | None = None,
        exc: BaseException | None = None,
        retryability: Retryability | None = None,
    ) -> None:
        """source 単位の dispatch 結果を記録する。"""
        payload = DispatchPayload(
            source_name=source_name,
            cadence=cadence,
            raw_source_name=_limited(raw_source_name, _RAW_SOURCE_NAME_LIMIT),
            error_message=error_message_of(exc),
            error_chain=extract_error_chain(exc) if exc is not None else None,
        )
        await self._events.append(
            stage=Stage.DISPATCH,
            event_type=event_type,
            outcome_code=outcome_code.value,
            payload=payload,
            source_id=source_id,
            error_class=exception_fqn(exc) if exc is not None else None,
            retryability=retryability,
        )

    async def append_run_completed(self, *, cadence: DispatchCadence) -> None:
        """1 件以上 dispatch した run の成功 heartbeat を焼く (件数なし)。

        per-source occurrence は metric (``vector.dispatch.outcome``) に移設したため、
        本行は admin Pipeline Health の ``last_succeeded_at[dispatch]`` を維持する
        liveness 用の最小 succeeded 行のみを担う。
        """
        await self._events.append(
            stage=Stage.DISPATCH,
            event_type=EventType.SUCCEEDED,
            outcome_code=DispatchOutcomeCode.DISPATCH_RUN_COMPLETED.value,
            payload=DispatchPayload(cadence=cadence),
        )

    async def append_run_event(
        self,
        *,
        event_type: EventType,
        outcome_code: DispatchOutcomeCode,
        cadence: DispatchCadence,
        exc: BaseException | None = None,
        retryability: Retryability | None = None,
    ) -> None:
        """run 単位の失敗を記録する (成功 / 件数 summary は焼かない)。"""
        payload = DispatchPayload(
            cadence=cadence,
            error_message=error_message_of(exc),
            error_chain=extract_error_chain(exc) if exc is not None else None,
        )
        await self._events.append(
            stage=Stage.DISPATCH,
            event_type=event_type,
            outcome_code=outcome_code.value,
            payload=payload,
            error_class=exception_fqn(exc) if exc is not None else None,
            retryability=retryability,
        )


def _limited(value: str | None, limit: int) -> str | None:
    if value is None:
        return None
    return redact_secrets(value)[:limit] or None
