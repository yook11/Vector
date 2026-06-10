"""Trend discovery stage の監査イベントを組み立てる。"""

from __future__ import annotations

from datetime import date
from enum import StrEnum
from typing import Literal

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.audit.domain.event import EventType, Stage
from app.audit.domain.payloads import TrendDiscoveryPayload
from app.audit.error_chain import extract_error_chain
from app.audit.error_fields import error_message_of, exception_fqn
from app.audit.failure_projection import Retryability
from app.audit.repository import PipelineEventRepository

TrendDiscoveryTrigger = Literal["cron", "cli"]

logger = structlog.get_logger(__name__)


class TrendDiscoveryOutcomeCode(StrEnum):
    """Stage.TREND_DISCOVERY の outcome code。"""

    RUN_COMPLETED = "trend_discovery_run_completed"
    RUN_UPDATED = "trend_discovery_run_updated"
    RUN_NO_TARGET_ARTICLES = "trend_discovery_run_no_target_articles"
    RUN_ALREADY_EXISTS = "trend_discovery_run_already_exists"
    RUN_CONFLICT = "trend_discovery_run_conflict"
    RUN_FAILED = "trend_discovery_run_failed"


class TrendDiscoveryAuditRepository:
    """Trend discovery 専用の payload / outcome_code を決める。"""

    def __init__(self, session: AsyncSession) -> None:
        self._events = PipelineEventRepository(session)

    async def append_run_event(
        self,
        *,
        event_type: EventType,
        outcome_code: TrendDiscoveryOutcomeCode,
        window_start: date,
        window_end: date,
        trigger: TrendDiscoveryTrigger,
        requested_update: bool,
        source_analysis_count: int | None = None,
        completed_category_count: int | None = None,
        exc: BaseException | None = None,
        retryability: Retryability | None = None,
    ) -> None:
        """run 単位の trend discovery 結果を記録する。"""
        payload = TrendDiscoveryPayload(
            window_start=window_start.isoformat(),
            window_end=window_end.isoformat(),
            trigger=trigger,
            requested_update=requested_update,
            source_analysis_count=source_analysis_count,
            completed_category_count=completed_category_count,
            error_message=error_message_of(exc),
            error_chain=extract_error_chain(exc) if exc is not None else None,
        )
        resolved_retryability = retryability
        if event_type == EventType.FAILED and resolved_retryability is None:
            resolved_retryability = Retryability.UNKNOWN
        await self._events.append(
            stage=Stage.TREND_DISCOVERY,
            event_type=event_type,
            outcome_code=outcome_code.value,
            payload=payload,
            error_class=exception_fqn(exc) if exc is not None else None,
            retryability=resolved_retryability,
        )


async def append_trend_discovery_run_event_best_effort(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    event_type: EventType,
    outcome_code: TrendDiscoveryOutcomeCode,
    window_start: date,
    window_end: date,
    trigger: TrendDiscoveryTrigger,
    requested_update: bool,
    source_analysis_count: int | None = None,
    completed_category_count: int | None = None,
    exc: BaseException | None = None,
) -> None:
    """run 単位監査を best-effort で焼く。"""
    try:
        async with session_factory() as session:
            await TrendDiscoveryAuditRepository(session).append_run_event(
                event_type=event_type,
                outcome_code=outcome_code,
                window_start=window_start,
                window_end=window_end,
                trigger=trigger,
                requested_update=requested_update,
                source_analysis_count=source_analysis_count,
                completed_category_count=completed_category_count,
                exc=exc,
            )
            await session.commit()
    except Exception as audit_exc:  # noqa: BLE001
        logger.exception(
            "trend_discovery_run_audit_dropped",
            outcome_code=outcome_code.value,
            trigger=trigger,
            window_end=window_end.isoformat(),
            audit_error_class=exception_fqn(audit_exc),
        )
