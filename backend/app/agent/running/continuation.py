"""Agent run executionの継続権を、Runの状態を変える操作で確認する。"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from uuid import UUID

from app.agent.live_updates.metrics import (
    record_agent_run_execution_probe_unavailable,
)
from app.agent.running.state import (
    AttemptSuperseded,
    CancelledByUser,
    CanContinue,
    CannotContinue,
    DeadlineExceeded,
    RunNotFound,
)
from app.agent.running.state_service import AgentRunStateService
from app.agent.running.stop import AnswerGenerationStopped
from app.log_policy.policies.research import RESEARCH_LOG_RULES
from app.log_policy.runtime import create_policy_json_logger

AGENT_RUN_EXECUTION_PROBE_INTERVAL_SECONDS = 2.0

logger = create_policy_json_logger(__name__, RESEARCH_LOG_RULES)


class AgentRunExecutionProbe:
    """同じrun attemptが継続できなくなったら AnswerGenerationStopped を送出する。"""

    def __init__(
        self,
        state_service: AgentRunStateService,
        run_id: UUID,
        attempt_epoch: int,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._state_service = state_service
        self._run_id = run_id
        self._attempt_epoch = attempt_epoch
        self._clock = clock
        self._lock = asyncio.Lock()
        self._last_check_at: float | None = None
        self._stopped: CannotContinue | None = None

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
                checked = await self._state_service.check_continuation(
                    self._run_id, attempt_epoch=self._attempt_epoch
                )
            except Exception:
                logger.warning(
                    "agent_run_execution_probe_unavailable",
                    run_id=str(self._run_id),
                    attempt_epoch=self._attempt_epoch,
                )
                record_agent_run_execution_probe_unavailable()
                checked = CanContinue()

            self._last_check_at = checked_at
            match checked:
                case CanContinue():
                    return
                case (
                    CancelledByUser()
                    | DeadlineExceeded()
                    | AttemptSuperseded()
                    | RunNotFound()
                ) as stopped:
                    self._stopped = stopped
                    raise AnswerGenerationStopped(stopped)
