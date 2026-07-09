"""Repository for agent conversation history and run state."""

from __future__ import annotations

import uuid as uuid_mod
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.contract import AnswerQuestionResult
from app.agent.history.mapper import (
    build_assistant_message_for_result,
    build_source_rows_for_message,
)
from app.agent.history.projection import (
    build_research_response_from_rows,
    build_research_run_response,
)
from app.agent.history.types import AgentRunErrorCode, AgentRunStatus
from app.models.agent_message import AgentMessage, AgentMessageSource
from app.models.agent_run import AgentRun
from app.models.agent_thread import AgentThread
from app.schemas.research import ResearchResponse, ResearchRunResponse


class ThreadNotFoundError(Exception):
    """Requested thread is missing or not owned by the current user."""


class ActiveRunConflictError(Exception):
    """A queued/running run already exists for the requested thread."""


class RunTransitionLostError(Exception):
    """Another actor moved the run before this transition could commit."""


@dataclass(frozen=True, slots=True)
class CreatedAgentRun:
    thread_id: uuid_mod.UUID
    run_id: uuid_mod.UUID


@dataclass(frozen=True, slots=True)
class PreparedAgentRun:
    run_id: uuid_mod.UUID
    thread_id: uuid_mod.UUID
    question: str


_ACTIVE_STATUSES = (AgentRunStatus.QUEUED.value, AgentRunStatus.RUNNING.value)
_TERMINAL_STATUSES = (AgentRunStatus.COMPLETED.value, AgentRunStatus.FAILED.value)
_STALE_AFTER = timedelta(minutes=20)


class AgentHistoryRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create_user_run(
        self,
        *,
        user_id: uuid_mod.UUID,
        question: str,
        thread_id: uuid_mod.UUID | None,
        now: datetime | None = None,
    ) -> CreatedAgentRun:
        now = now or datetime.now(UTC)
        if thread_id is None:
            thread = AgentThread(
                user_id=user_id,
                title=question[:50],
                updated_at=now,
            )
            self._session.add(thread)
            await self._session.flush()
            next_seq = 1
        else:
            thread = (
                await self._session.execute(
                    select(AgentThread)
                    .where(
                        AgentThread.id == thread_id,
                        AgentThread.user_id == user_id,
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if thread is None:
                raise ThreadNotFoundError()
            if await self._has_active_run(thread.id):
                raise ActiveRunConflictError()
            next_seq = await self._next_message_seq(thread.id)
            thread.updated_at = now

        user_message = AgentMessage(
            thread_id=thread.id,
            seq=next_seq,
            role="user",
            content=question,
            missing_aspects=[],
        )
        self._session.add(user_message)
        await self._session.flush()

        run = AgentRun(
            thread_id=thread.id,
            user_message_id=user_message.id,
            status=AgentRunStatus.QUEUED.value,
        )
        self._session.add(run)
        await self._session.flush()
        return CreatedAgentRun(thread_id=thread.id, run_id=run.id)

    async def mark_failed(
        self,
        run_id: uuid_mod.UUID,
        *,
        error_code: AgentRunErrorCode,
        now: datetime | None = None,
    ) -> bool:
        now = now or datetime.now(UTC)
        result = await self._session.execute(
            update(AgentRun)
            .where(
                AgentRun.id == run_id,
                AgentRun.status.in_(_ACTIVE_STATUSES),
            )
            .values(
                status=AgentRunStatus.FAILED.value,
                error_code=error_code.value,
                completed_at=now,
            )
            .execution_options(synchronize_session=False)
        )
        return (result.rowcount or 0) == 1

    async def mark_enqueue_failed(
        self,
        run_id: uuid_mod.UUID,
        *,
        now: datetime | None = None,
    ) -> bool:
        now = now or datetime.now(UTC)
        result = await self._session.execute(
            update(AgentRun)
            .where(
                AgentRun.id == run_id,
                AgentRun.status == AgentRunStatus.QUEUED.value,
            )
            .values(
                status=AgentRunStatus.FAILED.value,
                error_code=AgentRunErrorCode.ENQUEUE_FAILED.value,
                completed_at=now,
            )
            .execution_options(synchronize_session=False)
        )
        return (result.rowcount or 0) == 1

    async def acquire_for_execution(
        self,
        run_id: uuid_mod.UUID,
        *,
        now: datetime | None = None,
    ) -> PreparedAgentRun | None:
        now = now or datetime.now(UTC)
        row = (
            await self._session.execute(
                select(AgentRun, AgentMessage.content)
                .join(AgentMessage, AgentRun.user_message_id == AgentMessage.id)
                .where(AgentRun.id == run_id)
            )
        ).one_or_none()
        if row is None:
            return None
        run, question = row
        if run.status in _TERMINAL_STATUSES:
            return None
        result = await self._session.execute(
            update(AgentRun)
            .where(
                AgentRun.id == run_id,
                AgentRun.status.in_(_ACTIVE_STATUSES),
            )
            .values(status=AgentRunStatus.RUNNING.value, started_at=now)
            .execution_options(synchronize_session=False)
        )
        if (result.rowcount or 0) != 1:
            return None
        return PreparedAgentRun(
            run_id=run.id,
            thread_id=run.thread_id,
            question=question,
        )

    async def complete_run(
        self,
        *,
        run_id: uuid_mod.UUID,
        result: AnswerQuestionResult,
        now: datetime | None = None,
    ) -> bool:
        now = now or datetime.now(UTC)
        run = await self._session.get(AgentRun, run_id)
        if run is None or run.status in _TERMINAL_STATUSES:
            return False
        thread = (
            await self._session.execute(
                select(AgentThread)
                .where(AgentThread.id == run.thread_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if thread is None:
            return False

        assistant_message = build_assistant_message_for_result(
            thread_id=thread.id,
            seq=await self._next_message_seq(thread.id),
            result=result,
        )
        self._session.add(assistant_message)
        await self._session.flush()
        self._session.add_all(build_source_rows_for_message(assistant_message, result))
        await self._session.flush()

        update_result = await self._session.execute(
            update(AgentRun)
            .where(
                AgentRun.id == run_id,
                AgentRun.status == AgentRunStatus.RUNNING.value,
            )
            .values(
                status=AgentRunStatus.COMPLETED.value,
                assistant_message_id=assistant_message.id,
                completed_at=now,
            )
            .execution_options(synchronize_session=False)
        )
        if (update_result.rowcount or 0) != 1:
            raise RunTransitionLostError()
        thread.updated_at = now
        return True

    async def read_run_for_user(
        self,
        *,
        run_id: uuid_mod.UUID,
        user_id: uuid_mod.UUID,
    ) -> ResearchRunResponse | None:
        run = (
            await self._session.execute(
                select(AgentRun)
                .join(AgentThread, AgentRun.thread_id == AgentThread.id)
                .where(
                    AgentRun.id == run_id,
                    AgentThread.user_id == user_id,
                )
            )
        ).scalar_one_or_none()
        if run is None:
            return None

        result: ResearchResponse | None = None
        if run.status == AgentRunStatus.COMPLETED.value:
            result = await self._read_completed_result(run)
        return build_research_run_response(run=run, result=result)

    async def sweep_stale_runs(
        self,
        *,
        now: datetime | None = None,
    ) -> int:
        now = now or datetime.now(UTC)
        cutoff = now - _STALE_AFTER
        result = await self._session.execute(
            update(AgentRun)
            .where(
                AgentRun.status.in_(_ACTIVE_STATUSES),
                func.coalesce(AgentRun.started_at, AgentRun.created_at) < cutoff,
            )
            .values(
                status=AgentRunStatus.FAILED.value,
                error_code=AgentRunErrorCode.STALE.value,
                completed_at=now,
            )
            .execution_options(synchronize_session=False)
        )
        return result.rowcount or 0

    async def _has_active_run(self, thread_id: uuid_mod.UUID) -> bool:
        return (
            await self._session.execute(
                select(AgentRun.id)
                .where(
                    AgentRun.thread_id == thread_id,
                    AgentRun.status.in_(_ACTIVE_STATUSES),
                )
                .limit(1)
            )
        ).scalar_one_or_none() is not None

    async def _next_message_seq(self, thread_id: uuid_mod.UUID) -> int:
        value = (
            await self._session.execute(
                select(func.coalesce(func.max(AgentMessage.seq), 0) + 1).where(
                    AgentMessage.thread_id == thread_id
                )
            )
        ).scalar_one()
        return int(value)

    async def _read_completed_result(self, run: AgentRun) -> ResearchResponse | None:
        if run.assistant_message_id is None:
            return None
        message = await self._session.get(AgentMessage, run.assistant_message_id)
        if message is None:
            return None
        source_rows = (
            (
                await self._session.execute(
                    select(AgentMessageSource)
                    .where(AgentMessageSource.message_id == message.id)
                    .order_by(AgentMessageSource.ordinal)
                )
            )
            .scalars()
            .all()
        )
        return build_research_response_from_rows(message=message, sources=source_rows)
