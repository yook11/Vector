"""工程に入る前の継続確認と段階の通知を、Runの状態を変える操作で行う。"""

from __future__ import annotations

from uuid import UUID

from app.agent.contract import AnsweringStage
from app.agent.live_updates.reporters import AgentRunLiveStageReporter
from app.agent.running.state import (
    AttemptSuperseded,
    CancelledByUser,
    CanContinue,
    CannotContinue,
    DeadlineExceeded,
    RunNotFound,
)
from app.agent.running.state_service import AgentRunStateService

__all__ = ["AgentRunStageGate"]


class AgentRunStageGate:
    def __init__(
        self,
        state_service: AgentRunStateService,
        run_id: UUID,
        attempt_epoch: int,
        *,
        stage_reporter: AgentRunLiveStageReporter,
    ) -> None:
        self._state_service = state_service
        self._run_id = run_id
        self._attempt_epoch = attempt_epoch
        self._stage_reporter = stage_reporter

    async def enter(self, stage: AnsweringStage) -> CanContinue | CannotContinue:
        match await self._state_service.authorize_stage_entry(
            self._run_id, attempt_epoch=self._attempt_epoch
        ):
            case CanContinue() as can_continue:
                await self._stage_reporter.stage_changed(stage)
                return can_continue
            case (
                CancelledByUser()
                | DeadlineExceeded()
                | AttemptSuperseded()
                | RunNotFound()
            ) as stopped:
                return stopped
