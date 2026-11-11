"""工程に入る前の継続確認と段階の通知を、Runの状態を変える操作で行う。"""

from __future__ import annotations

from app.agent.contract import AnsweringStage
from app.agent.live_updates.reporters import AgentRunLiveStageReporter
from app.agent.running.state import CanContinue, CannotContinue
from app.agent.running.state_service import AgentRunStateService
from app.agent.running.types import RunAttempt

__all__ = ["AgentRunStageGate"]


class AgentRunStageGate:
    def __init__(
        self,
        state_service: AgentRunStateService,
        attempt: RunAttempt,
        *,
        stage_reporter: AgentRunLiveStageReporter,
    ) -> None:
        self._state_service = state_service
        self._attempt = attempt
        self._stage_reporter = stage_reporter

    async def enter(self, stage: AnsweringStage) -> CanContinue | CannotContinue:
        match await self._state_service.authorize_continuation(self._attempt):
            case CanContinue() as can_continue:
                await self._stage_reporter.stage_changed(stage)
                return can_continue
            case CannotContinue() as stopped:
                return stopped
