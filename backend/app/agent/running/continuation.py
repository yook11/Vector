"""Agent run executionの継続権を短命DB sessionで確認する。"""

from __future__ import annotations

import asyncio
import time
import uuid as uuid_mod
from collections.abc import Callable
from datetime import datetime
from uuid import UUID

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent.live_updates.metrics import (
    record_agent_run_execution_probe_unavailable,
)
from app.agent.running.deadline.deadline_exceeded import database_now, expire_run
from app.agent.runs.execution import Continue, Stop, StopReason
from app.agent.runs.types import AgentRunStatus
from app.models.agent_run import AgentRun

AGENT_RUN_EXECUTION_PROBE_INTERVAL_SECONDS = 2.0

logger = structlog.get_logger(__name__)


class AgentRunContinuationRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def decide_execution_continuation(
        self,
        *,
        run_id: uuid_mod.UUID,
        attempt_epoch: int,
        now: datetime | None = None,
    ) -> Continue | Stop:
        now = await database_now(self._session, now)
        deadline_at = (
            await self._session.execute(
                select(AgentRun.deadline_at).where(
                    AgentRun.id == run_id,
                    AgentRun.status == AgentRunStatus.RUNNING.value,
                    AgentRun.attempt_epoch == attempt_epoch,
                )
            )
        ).scalar_one_or_none()
        if deadline_at is None:
            return Stop(StopReason.NOT_CURRENT)
        if now < deadline_at:
            return Continue()

        expired = await expire_run(
            self._session,
            run_id=run_id,
            expected_status=AgentRunStatus.RUNNING,
            expected_attempt_epoch=attempt_epoch,
            now=now,
        )
        if not expired:
            return Stop(StopReason.NOT_CURRENT)
        return Stop(StopReason.DEADLINE_EXCEEDED)


class AgentRunExecutionProbe:
    """同じrun attemptが継続できる間だけ Continue を返す。"""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        run_id: UUID,
        attempt_epoch: int,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._session_factory = session_factory
        self._run_id = run_id
        self._attempt_epoch = attempt_epoch
        self._clock = clock
        self._lock = asyncio.Lock()
        self._last_check_at: float | None = None
        self._cached: Continue | Stop | None = None

    async def should_continue(self) -> Continue | Stop:
        async with self._lock:
            if self._cached is not None:
                if isinstance(self._cached, Stop):
                    return self._cached
                if (
                    self._last_check_at is not None
                    and self._clock() - self._last_check_at
                    < AGENT_RUN_EXECUTION_PROBE_INTERVAL_SECONDS
                ):
                    return self._cached

            checked_at = self._clock()
            try:
                async with self._session_factory() as session:
                    async with session.begin():
                        result = await AgentRunContinuationRepository(
                            session
                        ).decide_execution_continuation(
                            run_id=self._run_id,
                            attempt_epoch=self._attempt_epoch,
                        )
            except Exception:
                logger.warning(
                    "agent_run_execution_probe_unavailable",
                    run_id=str(self._run_id),
                    attempt_epoch=self._attempt_epoch,
                )
                record_agent_run_execution_probe_unavailable()
                result = Continue()

            self._cached = result
            self._last_check_at = checked_at
            return result
