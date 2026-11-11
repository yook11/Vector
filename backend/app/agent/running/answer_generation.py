"""回答の生成の開始・書き直しを、Runの状態を変える操作で問い合わせる。"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from uuid import UUID

from app.agent.answering import timing as answer_timing
from app.agent.live_updates.reporters import AgentRunLiveStageReporter
from app.agent.running.state import (
    AnswerGenerationStarted,
    CanContinue,
    CannotContinue,
)
from app.agent.running.state_service import AgentRunStateService
from app.agent.running.types import RunAttempt

__all__ = ["AgentRunAnswerGenerationGate"]


class AgentRunAnswerGenerationGate:
    def __init__(
        self,
        state_service: AgentRunStateService,
        attempt: RunAttempt,
        *,
        schedule_deadline_check: Callable[[UUID, datetime], None],
        stage_reporter: AgentRunLiveStageReporter,
    ) -> None:
        self._state_service = state_service
        self._attempt = attempt
        self._schedule_deadline_check = schedule_deadline_check
        self._stage_reporter = stage_reporter

    async def start(self) -> CanContinue | CannotContinue:
        """開始を記録できたら、回収期限の確認を予約して「回答中」を通知する。"""
        match await self._state_service.start_answer_generation(self._attempt):
            case AnswerGenerationStarted(
                run_id=run_id, answer_started_at=answer_started_at
            ):
                recovery_window = answer_timing.answer_generation_recovery_window()
                self._schedule_deadline_check(
                    run_id, answer_started_at + recovery_window
                )
                await self._stage_reporter.stage_changed("answering")
                return CanContinue()
            case CannotContinue() as stopped:
                return stopped

    async def regenerate(self) -> CanContinue | CannotContinue:
        return await self._state_service.authorize_continuation(self._attempt)
