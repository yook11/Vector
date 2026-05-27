"""Dispatch stage の監査イベントを組み立てる。"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.domain.event import EventType, Stage
from app.audit.domain.payloads import DispatchPayload
from app.audit.error_chain import extract_error_chain
from app.audit.failure_projection import Retryability
from app.audit.repository import PipelineEventRepository
from app.shared.security.redaction import redact_secrets

DispatchCadence = Literal["high", "medium", "low", "all"]

_ERROR_MESSAGE_LIMIT = 2000
_RAW_SOURCE_NAME_LIMIT = 200


class DispatchOutcomeCode(StrEnum):
    """Stage.DISPATCH の outcome code。"""

    SOURCE_DISPATCHED = "source_dispatched"
    SOURCE_ENQUEUE_FAILED = "source_enqueue_failed"
    SOURCE_NOT_REGISTERED = "source_not_registered"
    SOURCE_NAME_INVALID = "source_name_invalid"
    DISPATCH_RUN_NO_TARGETS = "dispatch_run_no_targets"
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
            error_message=_error_message(exc),
            error_chain=extract_error_chain(exc) if exc is not None else None,
        )
        await self._events.append(
            stage=Stage.DISPATCH,
            event_type=event_type,
            outcome_code=outcome_code.value,
            payload=payload,
            source_id=source_id,
            error_class=_fqn(exc) if exc is not None else None,
            retryability=retryability,
        )

    async def append_run_event(
        self,
        *,
        event_type: EventType,
        outcome_code: DispatchOutcomeCode,
        cadence: DispatchCadence,
        selected_count: int | None = None,
        dispatched_count: int | None = None,
        rejected_count: int | None = None,
        failed_count: int | None = None,
        exc: BaseException | None = None,
        retryability: Retryability | None = None,
    ) -> None:
        """run 単位の dispatch 結果を記録する。"""
        payload = DispatchPayload(
            cadence=cadence,
            selected_count=selected_count,
            dispatched_count=dispatched_count,
            rejected_count=rejected_count,
            failed_count=failed_count,
            error_message=_error_message(exc),
            error_chain=extract_error_chain(exc) if exc is not None else None,
        )
        await self._events.append(
            stage=Stage.DISPATCH,
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


def _limited(value: str | None, limit: int) -> str | None:
    if value is None:
        return None
    return redact_secrets(value)[:limit] or None


def _fqn(exc: BaseException) -> str:
    return f"{type(exc).__module__}.{type(exc).__qualname__}"
