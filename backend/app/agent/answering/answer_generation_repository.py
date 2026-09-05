"""回答生成の開始・再生成許可・継続確認を短命DB transactionで行う。"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from datetime import datetime
from typing import Protocol
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent.run_deadline.persistence import database_now
from app.agent.runs.execution import Continue, Stop, StopReason
from app.agent.runs.types import AgentRunStatus
from app.models.agent_run import AgentRun

__all__ = [
    "ANSWER_GENERATION_CONTINUATION_INTERVAL_SECONDS",
    "AgentAnswerGenerationRepository",
    "AnswerGenerationRepository",
]

ANSWER_GENERATION_CONTINUATION_INTERVAL_SECONDS = 2.0


class AnswerGenerationRepository(Protocol):
    async def start_answer_generation(self) -> Continue | Stop: ...

    async def authorize_answer_regeneration(self) -> Continue | Stop: ...

    async def check_answer_generation_continuation(self) -> Continue | Stop: ...


class AgentAnswerGenerationRepository:
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

    async def start_answer_generation(
        self, *, now: datetime | None = None
    ) -> Continue | Stop:
        async with self._session_factory() as session:
            async with session.begin():
                return await _start_answer_generation(
                    session,
                    run_id=self._run_id,
                    expected_attempt_epoch=self._attempt_epoch,
                    now=now,
                )

    async def authorize_answer_regeneration(
        self, *, now: datetime | None = None
    ) -> Continue | Stop:
        async with self._session_factory() as session:
            async with session.begin():
                return await _authorize_answer_regeneration(
                    session,
                    run_id=self._run_id,
                    expected_attempt_epoch=self._attempt_epoch,
                    now=now,
                )

    async def check_answer_generation_continuation(self) -> Continue | Stop:
        async with self._lock:
            if self._cached is not None:
                if isinstance(self._cached, Stop):
                    return self._cached
                if (
                    self._last_check_at is not None
                    and self._clock() - self._last_check_at
                    < ANSWER_GENERATION_CONTINUATION_INTERVAL_SECONDS
                ):
                    return self._cached

            checked_at = self._clock()
            async with self._session_factory() as session:
                async with session.begin():
                    result = await _check_answer_generation_continuation(
                        session,
                        run_id=self._run_id,
                        expected_attempt_epoch=self._attempt_epoch,
                    )
            self._cached = result
            self._last_check_at = checked_at
            return result


async def _lock_run(session: AsyncSession, run_id: UUID) -> AgentRun | None:
    return (
        await session.execute(
            select(AgentRun)
            .where(AgentRun.id == run_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()


def _is_current_running(run: AgentRun | None, expected_attempt_epoch: int) -> bool:
    return (
        run is not None
        and run.status == AgentRunStatus.RUNNING.value
        and run.attempt_epoch == expected_attempt_epoch
    )


def _expire_for_deadline(run: AgentRun) -> Stop:
    run.status = AgentRunStatus.DEADLINE_EXCEEDED.value
    run.assistant_message_id = None
    run.error_code = None
    return Stop(StopReason.DEADLINE_EXCEEDED)


async def _start_answer_generation(
    session: AsyncSession,
    *,
    run_id: UUID,
    expected_attempt_epoch: int,
    now: datetime | None = None,
) -> Continue | Stop:
    run = await _lock_run(session, run_id)
    if (
        run is None
        or run.status != AgentRunStatus.RUNNING.value
        or run.attempt_epoch != expected_attempt_epoch
        or run.answer_started_at is not None
    ):
        return Stop(StopReason.NOT_CURRENT)

    now = await database_now(session, now)
    if now >= run.deadline_at:
        return _expire_for_deadline(run)

    run.answer_started_at = now
    return Continue()


async def _authorize_answer_regeneration(
    session: AsyncSession,
    *,
    run_id: UUID,
    expected_attempt_epoch: int,
    now: datetime | None = None,
) -> Continue | Stop:
    run = await _lock_run(session, run_id)
    if not _is_current_running(run, expected_attempt_epoch) or (
        run is None or run.answer_started_at is None
    ):
        return Stop(StopReason.NOT_CURRENT)

    now = await database_now(session, now)
    if now >= run.deadline_at:
        return _expire_for_deadline(run)
    return Continue()


async def _check_answer_generation_continuation(
    session: AsyncSession,
    *,
    run_id: UUID,
    expected_attempt_epoch: int,
) -> Continue | Stop:
    run = (
        await session.execute(select(AgentRun).where(AgentRun.id == run_id))
    ).scalar_one_or_none()
    if (
        not _is_current_running(run, expected_attempt_epoch)
        or run is None
        or run.answer_started_at is None
    ):
        return Stop(StopReason.NOT_CURRENT)
    return Continue()
