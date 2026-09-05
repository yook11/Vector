from __future__ import annotations

import uuid as uuid_mod

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.runs.types import AgentRunStatus
from app.models.agent_run import AgentRun


class AgentRunPolicyBlockRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def mark_policy_blocked(
        self,
        run_id: uuid_mod.UUID,
        *,
        expected_attempt_epoch: int,
    ) -> bool:
        result = await self._session.execute(
            update(AgentRun)
            .where(
                AgentRun.id == run_id,
                AgentRun.status == AgentRunStatus.RUNNING.value,
                AgentRun.attempt_epoch == expected_attempt_epoch,
            )
            .values(
                status=AgentRunStatus.POLICY_BLOCKED.value,
                assistant_message_id=None,
                error_code=None,
            )
            .execution_options(synchronize_session=False)
        )
        return (result.rowcount or 0) == 1
