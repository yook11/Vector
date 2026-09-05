from __future__ import annotations

import uuid as uuid_mod
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.runs.projection import build_research_run_response
from app.agent.runs.types import AgentRunErrorCode, AgentRunStatus
from app.models.agent_run import AgentRun
from app.models.agent_thread import AgentThread
from app.schemas.research import ResearchRunResponse


@dataclass(frozen=True, slots=True)
class OwnedAgentRunLiveContext:
    run_id: UUID
    status: AgentRunStatus
    attempt_epoch: int
    error_code: AgentRunErrorCode | None


class AgentRunPresentationRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

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

    async def read_live_context_for_user(
        self,
        *,
        run_id: uuid_mod.UUID,
        user_id: uuid_mod.UUID,
    ) -> OwnedAgentRunLiveContext | None:
        row = (
            await self._session.execute(
                select(
                    AgentRun.id,
                    AgentRun.status,
                    AgentRun.attempt_epoch,
                    AgentRun.error_code,
                )
                .join(AgentThread, AgentRun.thread_id == AgentThread.id)
                .where(
                    AgentRun.id == run_id,
                    AgentThread.user_id == user_id,
                )
            )
        ).one_or_none()
        if row is None:
            return None
        context_run_id, status_value, attempt_epoch, error_code = row
        return OwnedAgentRunLiveContext(
            run_id=context_run_id,
            status=AgentRunStatus(status_value),
            attempt_epoch=attempt_epoch,
            error_code=(AgentRunErrorCode(error_code) if error_code else None),
        )
