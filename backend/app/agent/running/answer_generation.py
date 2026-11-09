"""回答生成の開始・再生成許可を、Runの状態を変える操作で行う。"""

from __future__ import annotations

from typing import Protocol
from uuid import UUID

from app.agent.running.state import (
    AnswerGenerationStarted,
    AttemptSuperseded,
    CancelledByUser,
    CanContinue,
    DeadlineExceeded,
    RunNotFound,
)
from app.agent.running.state_service import AgentRunStateService
from app.agent.running.stop import AnswerGenerationStopped

__all__ = [
    "AgentAnswerGenerationRepository",
    "AnswerGenerationRepository",
]


class AnswerGenerationRepository(Protocol):
    async def start_answer_generation(self) -> AnswerGenerationStarted: ...

    async def authorize_answer_regeneration(self) -> None: ...


class AgentAnswerGenerationRepository:
    def __init__(
        self,
        state_service: AgentRunStateService,
        run_id: UUID,
        attempt_epoch: int,
    ) -> None:
        self._state_service = state_service
        self._run_id = run_id
        self._attempt_epoch = attempt_epoch

    async def start_answer_generation(self) -> AnswerGenerationStarted:
        match await self._state_service.start_answer_generation(
            self._run_id, attempt_epoch=self._attempt_epoch
        ):
            case AnswerGenerationStarted() as started:
                return started
            case (
                CancelledByUser()
                | DeadlineExceeded()
                | AttemptSuperseded()
                | RunNotFound()
            ) as stopped:
                raise AnswerGenerationStopped(stopped)

    async def authorize_answer_regeneration(self) -> None:
        match await self._state_service.authorize_answer_regeneration(
            self._run_id, attempt_epoch=self._attempt_epoch
        ):
            case CanContinue():
                return
            case (
                CancelledByUser()
                | DeadlineExceeded()
                | AttemptSuperseded()
                | RunNotFound()
            ) as stopped:
                raise AnswerGenerationStopped(stopped)
