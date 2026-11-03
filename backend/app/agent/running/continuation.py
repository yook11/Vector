"""Agent run executionの継続権を短命DB sessionで確認する。"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent.live_updates.metrics import (
    record_agent_run_execution_probe_unavailable,
)
from app.agent.running.state import RunFinished, RunRejected
from app.agent.running.state_repository import AgentRunStateRepository
from app.agent.running.stop import AnswerGenerationStopped
from app.log_policy.policies.research import RESEARCH_LOG_RULES
from app.log_policy.runtime import create_policy_json_logger

AGENT_RUN_EXECUTION_PROBE_INTERVAL_SECONDS = 2.0

logger = create_policy_json_logger(__name__, RESEARCH_LOG_RULES)


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
        self._stopped: RunFinished | RunRejected | None = None

    async def ensure_can_continue(self) -> None:
        async with self._lock:
            if self._stopped is not None:
                raise AnswerGenerationStopped(self._stopped)
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
                        stopped = await AgentRunStateRepository(
                            session
                        ).check_continuation(
                            self._run_id,
                            attempt_epoch=self._attempt_epoch,
                        )
            except Exception:
                logger.warning(
                    "agent_run_execution_probe_unavailable",
                    run_id=str(self._run_id),
                    attempt_epoch=self._attempt_epoch,
                )
                record_agent_run_execution_probe_unavailable()
                stopped = None

            self._stopped = stopped
            self._last_check_at = checked_at
            if stopped is not None:
                raise AnswerGenerationStopped(stopped)
