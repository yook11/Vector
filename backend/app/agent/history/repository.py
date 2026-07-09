"""Repository for agent conversation history and run state."""

from __future__ import annotations

import uuid as uuid_mod
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Literal

import structlog
from sqlalchemy import delete, exists, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.contract import AnswerQuestionResult
from app.agent.history.citation_integrity import assess_citation_integrity
from app.agent.history.mapper import (
    build_assistant_message_for_result,
    build_source_rows_for_message,
)
from app.agent.history.projection import (
    build_research_run_response,
    build_research_thread_detail,
    build_research_thread_list_item,
)
from app.agent.history.types import AgentRunErrorCode, AgentRunStatus
from app.models.agent_message import AgentMessage, AgentMessageSource
from app.models.agent_run import AgentRun
from app.models.agent_thread import AgentThread
from app.schemas.research import (
    PaginatedResearchThreadResponse,
    ResearchRunResponse,
    ResearchThreadDetail,
    ResearchThreadListParams,
)


class ThreadNotFoundError(Exception):
    """Requested thread is missing or not owned by the current user."""


class ActiveRunConflictError(Exception):
    """A queued/running run already exists for the requested thread."""


class RunTransitionLostError(Exception):
    """Another actor moved the run before this transition could commit."""


class CancelRunOutcome(StrEnum):
    CANCELLED = "cancelled"
    ALREADY_FAILED = "already_failed"
    ALREADY_COMPLETED = "already_completed"


@dataclass(frozen=True, slots=True)
class CreatedAgentRun:
    thread_id: uuid_mod.UUID
    run_id: uuid_mod.UUID


@dataclass(frozen=True, slots=True)
class PreparedAgentRun:
    run_id: uuid_mod.UUID
    thread_id: uuid_mod.UUID
    question: str
    user_message_seq: int


@dataclass(frozen=True, slots=True)
class ThreadMessageSnapshot:
    """Resolution に渡す、thread 内メッセージの最小投影。"""

    role: Literal["user", "assistant"]
    content: str


_ACTIVE_STATUSES = (AgentRunStatus.QUEUED.value, AgentRunStatus.RUNNING.value)
_TERMINAL_STATUSES = (AgentRunStatus.COMPLETED.value, AgentRunStatus.FAILED.value)
_STALE_AFTER = timedelta(minutes=20)
logger = structlog.get_logger(__name__)


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
                select(AgentRun, AgentMessage.content, AgentMessage.seq)
                .join(AgentMessage, AgentRun.user_message_id == AgentMessage.id)
                .where(AgentRun.id == run_id)
            )
        ).one_or_none()
        if row is None:
            return None
        run, question, user_message_seq = row
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
            user_message_seq=user_message_seq,
        )

    async def read_recent_messages_before(
        self,
        *,
        thread_id: uuid_mod.UUID,
        before_seq: int,
        limit: int,
    ) -> list[ThreadMessageSnapshot]:
        """Return up to ``limit`` prior messages in chronological order."""

        if limit <= 0:
            return []
        rows = (
            await self._session.execute(
                select(AgentMessage.role, AgentMessage.content)
                .where(
                    AgentMessage.thread_id == thread_id,
                    AgentMessage.seq < before_seq,
                )
                .order_by(AgentMessage.seq.desc())
                .limit(limit)
            )
        ).all()
        snapshots: list[ThreadMessageSnapshot] = []
        for role, content in reversed(rows):
            if role not in {"user", "assistant"}:
                raise ValueError(f"unexpected agent message role: {role!r}")
            snapshots.append(ThreadMessageSnapshot(role=role, content=content))
        return snapshots

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
        source_rows = build_source_rows_for_message(assistant_message, result)
        _warn_on_citation_source_mismatch(
            run_id=run_id,
            answer=result.answer,
            source_refs=[row.source_ref for row in source_rows],
        )
        self._session.add_all(source_rows)
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
        return build_research_run_response(run=run)

    async def list_threads_for_user(
        self,
        *,
        user_id: uuid_mod.UUID,
        pagination: ResearchThreadListParams,
    ) -> PaginatedResearchThreadResponse:
        total = (
            await self._session.execute(
                select(func.count(AgentThread.id)).where(AgentThread.user_id == user_id)
            )
        ).scalar_one()
        has_active_run = exists(
            select(AgentRun.id).where(
                AgentRun.thread_id == AgentThread.id,
                AgentRun.status.in_(_ACTIVE_STATUSES),
            )
        )
        rows = (
            await self._session.execute(
                select(AgentThread, has_active_run.label("has_active_run"))
                .where(AgentThread.user_id == user_id)
                .order_by(AgentThread.updated_at.desc(), AgentThread.id.desc())
                .offset(pagination.offset)
                .limit(pagination.limit)
            )
        ).all()
        return PaginatedResearchThreadResponse.create(
            items=[
                build_research_thread_list_item(
                    thread=thread,
                    has_active_run=bool(has_active),
                )
                for thread, has_active in rows
            ],
            total=total,
            pagination=pagination,
        )

    async def read_thread_detail_for_user(
        self,
        *,
        thread_id: uuid_mod.UUID,
        user_id: uuid_mod.UUID,
    ) -> ResearchThreadDetail | None:
        thread = (
            await self._session.execute(
                select(AgentThread).where(
                    AgentThread.id == thread_id,
                    AgentThread.user_id == user_id,
                )
            )
        ).scalar_one_or_none()
        if thread is None:
            return None

        messages = (
            (
                await self._session.execute(
                    select(AgentMessage)
                    .where(AgentMessage.thread_id == thread.id)
                    .order_by(AgentMessage.seq)
                )
            )
            .scalars()
            .all()
        )
        user_message_ids = [
            message.id for message in messages if message.role == "user"
        ]
        runs_by_user_message_id: dict[uuid_mod.UUID, AgentRun] = {}
        if user_message_ids:
            runs = (
                (
                    await self._session.execute(
                        select(AgentRun).where(
                            AgentRun.thread_id == thread.id,
                            AgentRun.user_message_id.in_(user_message_ids),
                        )
                    )
                )
                .scalars()
                .all()
            )
            runs_by_user_message_id = {run.user_message_id: run for run in runs}

        assistant_message_ids = [
            message.id for message in messages if message.role == "assistant"
        ]
        sources_by_message_id: dict[uuid_mod.UUID, list[AgentMessageSource]] = {}
        if assistant_message_ids:
            sources = (
                (
                    await self._session.execute(
                        select(AgentMessageSource)
                        .where(AgentMessageSource.message_id.in_(assistant_message_ids))
                        .order_by(
                            AgentMessageSource.message_id,
                            AgentMessageSource.ordinal,
                        )
                    )
                )
                .scalars()
                .all()
            )
            for source in sources:
                sources_by_message_id.setdefault(source.message_id, []).append(source)

        return build_research_thread_detail(
            thread=thread,
            messages=messages,
            runs_by_user_message_id=runs_by_user_message_id,
            sources_by_message_id=sources_by_message_id,
        )

    async def delete_thread_for_user(
        self,
        *,
        thread_id: uuid_mod.UUID,
        user_id: uuid_mod.UUID,
    ) -> bool:
        result = await self._session.execute(
            delete(AgentThread)
            .where(
                AgentThread.id == thread_id,
                AgentThread.user_id == user_id,
            )
            .execution_options(synchronize_session=False)
        )
        return (result.rowcount or 0) == 1

    async def cancel_run_for_user(
        self,
        *,
        run_id: uuid_mod.UUID,
        user_id: uuid_mod.UUID,
        now: datetime | None = None,
    ) -> CancelRunOutcome | None:
        now = now or datetime.now(UTC)
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
        status_value = AgentRunStatus(run.status)
        if status_value is AgentRunStatus.COMPLETED:
            return CancelRunOutcome.ALREADY_COMPLETED
        if status_value is AgentRunStatus.FAILED:
            return CancelRunOutcome.ALREADY_FAILED

        result = await self._session.execute(
            update(AgentRun)
            .where(
                AgentRun.id == run_id,
                AgentRun.status.in_(_ACTIVE_STATUSES),
            )
            .values(
                status=AgentRunStatus.FAILED.value,
                error_code=AgentRunErrorCode.CANCELLED.value,
                completed_at=now,
            )
            .execution_options(synchronize_session=False)
        )
        if (result.rowcount or 0) == 1:
            return CancelRunOutcome.CANCELLED

        refreshed_status = (
            await self._session.execute(
                select(AgentRun.status)
                .join(AgentThread, AgentRun.thread_id == AgentThread.id)
                .where(
                    AgentRun.id == run_id,
                    AgentThread.user_id == user_id,
                )
            )
        ).scalar_one_or_none()
        if refreshed_status is None:
            return None
        if AgentRunStatus(refreshed_status) is AgentRunStatus.COMPLETED:
            return CancelRunOutcome.ALREADY_COMPLETED
        return CancelRunOutcome.ALREADY_FAILED

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


def _warn_on_citation_source_mismatch(
    *,
    run_id: uuid_mod.UUID,
    answer: str,
    source_refs: list[str],
) -> None:
    report = assess_citation_integrity(answer=answer, source_refs=source_refs)
    if not report.has_mismatch:
        return
    logger.warning(
        "agent_citation_source_mismatch",
        run_id=str(run_id),
        marker_without_source_refs=list(report.marker_without_source_refs),
        source_without_marker_refs=list(report.source_without_marker_refs),
    )
