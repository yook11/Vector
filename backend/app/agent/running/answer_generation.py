"""回答生成の開始・再生成許可を短命DB transactionで行う。"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent.running.state import AnswerGenerationStarted
from app.agent.running.state_repository import AgentRunStateRepository
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
        session_factory: async_sessionmaker[AsyncSession],
        run_id: UUID,
        attempt_epoch: int,
    ) -> None:
        self._session_factory = session_factory
        self._run_id = run_id
        self._attempt_epoch = attempt_epoch

    async def start_answer_generation(
        self, *, now: datetime | None = None
    ) -> AnswerGenerationStarted:
        async with self._session_factory() as session:
            async with session.begin():
                outcome = await AgentRunStateRepository(
                    session
                ).start_answer_generation(
                    self._run_id,
                    attempt_epoch=self._attempt_epoch,
                    now=now,
                )
        # 期限切れの確定をcommitしてから止めるため、transactionを閉じた後に送出する。
        if not isinstance(outcome, AnswerGenerationStarted):
            raise AnswerGenerationStopped(outcome)
        return outcome

    async def authorize_answer_regeneration(
        self, *, now: datetime | None = None
    ) -> None:
        async with self._session_factory() as session:
            async with session.begin():
                stopped = await AgentRunStateRepository(
                    session
                ).authorize_answer_regeneration(
                    self._run_id,
                    attempt_epoch=self._attempt_epoch,
                    now=now,
                )
        if stopped is not None:
            raise AnswerGenerationStopped(stopped)
