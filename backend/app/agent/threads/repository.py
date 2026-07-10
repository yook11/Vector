"""Repository for persisted agent thread reads and management."""

from __future__ import annotations

import uuid as uuid_mod

from sqlalchemy import delete, exists, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.runs.types import AgentRunStatus
from app.agent.threads.contracts import ThreadMessageSnapshot
from app.agent.threads.projection import (
    build_research_thread_detail,
    build_research_thread_list_item,
)
from app.models.agent_message import AgentMessage, AgentMessageSource
from app.models.agent_run import AgentRun
from app.models.agent_thread import AgentThread
from app.schemas.research import (
    PaginatedResearchThreadResponse,
    ResearchThreadDetail,
    ResearchThreadListParams,
)

_ACTIVE_STATUSES = (AgentRunStatus.QUEUED.value, AgentRunStatus.RUNNING.value)


class AgentThreadRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

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
