"""Agent run executionの継続権を短命DB sessionで確認する。"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from uuid import UUID

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent.live_updates.metrics import (
    record_agent_run_execution_probe_unavailable,
)
from app.agent.runs.repository import AgentRunRepository

AGENT_RUN_EXECUTION_PROBE_INTERVAL_SECONDS = 2.0

logger = structlog.get_logger(__name__)


class AgentRunExecutionProbe:
    """同じrun attemptがrunningである間だけ継続を許可する。"""

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
        self._terminal = False

    async def should_continue(self) -> bool:
        async with self._lock:
            if self._terminal:
                return False

            checked_at = self._clock()
            if (
                self._last_check_at is not None
                and checked_at - self._last_check_at
                < AGENT_RUN_EXECUTION_PROBE_INTERVAL_SECONDS
            ):
                return True

            self._last_check_at = checked_at
            try:
                async with self._session_factory() as session:
                    is_current = await AgentRunRepository(session).is_execution_current(
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
                return True

            if not is_current:
                self._terminal = True
            return is_current
