"""Agent run executionの継続権を短命DB sessionで確認する。"""

from __future__ import annotations

import asyncio
import time
import uuid as uuid_mod
from collections.abc import Callable
from datetime import datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent.contract import AnswerGenerationStopped
from app.agent.live_updates.metrics import (
    record_agent_run_execution_probe_unavailable,
)
from app.agent.running.deadline.deadline_exceeded import database_now, expire_run
from app.agent.runs.execution import StopReason
from app.agent.runs.types import AgentRunStatus
from app.log_policy.policies.research import RESEARCH_LOG_RULES
from app.log_policy.runtime import create_policy_json_logger
from app.models.agent_run import AgentRun

AGENT_RUN_EXECUTION_PROBE_INTERVAL_SECONDS = 2.0

logger = create_policy_json_logger(__name__, RESEARCH_LOG_RULES)


class AgentRunContinuationRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def decide_execution_continuation(
        self,
        *,
        run_id: uuid_mod.UUID,
        attempt_epoch: int,
        now: datetime | None = None,
    ) -> StopReason | None:
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
            return StopReason.NOT_CURRENT
        if now < deadline_at:
            return None

        expired = await expire_run(
            self._session,
            run_id=run_id,
            expected_status=AgentRunStatus.RUNNING,
            expected_attempt_epoch=attempt_epoch,
            now=now,
        )
        if not expired:
            return StopReason.NOT_CURRENT
        return StopReason.DEADLINE_EXCEEDED


class AgentRunExecutionProbe:
    """同じrun attemptが継続できなくなったら AnswerGenerationStopped を送出する。"""

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
        self._stop_reason: StopReason | None = None

    async def ensure_can_continue(self) -> None:
        async with self._lock:
            if self._stop_reason is not None:
                raise AnswerGenerationStopped(self._stop_reason)
            if (
                self._last_check_at is not None
                and self._clock() - self._last_check_at
                < AGENT_RUN_EXECUTION_PROBE_INTERVAL_SECONDS
            ):
                return

            checked_at = self._clock()
            try:
                async with self._session_factory() as session:
                    async with session.begin():
                        stop_reason = await AgentRunContinuationRepository(
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
                stop_reason = None

            self._stop_reason = stop_reason
            self._last_check_at = checked_at
            if stop_reason is not None:
                raise AnswerGenerationStopped(stop_reason)
