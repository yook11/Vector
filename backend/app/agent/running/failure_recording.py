"""実行中の失敗とキュー投入失敗を実行記録へ反映する。"""

from __future__ import annotations

import uuid as uuid_mod

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.runs.types import AgentRunErrorCode, AgentRunStatus
from app.models.agent_run import AgentRun

_ACTIVE_STATUSES = (AgentRunStatus.QUEUED.value, AgentRunStatus.RUNNING.value)


class AgentRunFailureRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def mark_failed(
        self,
        run_id: uuid_mod.UUID,
        *,
        expected_attempt_epoch: int,
        error_code: AgentRunErrorCode,
    ) -> bool:
        result = await self._session.execute(
            update(AgentRun)
            .where(
                AgentRun.id == run_id,
                AgentRun.status.in_(_ACTIVE_STATUSES),
                AgentRun.attempt_epoch == expected_attempt_epoch,
            )
            .values(
                status=AgentRunStatus.FAILED.value,
                error_code=error_code.value,
            )
            .execution_options(synchronize_session=False)
        )
        return (result.rowcount or 0) == 1

    async def mark_enqueue_failed(
        self,
        run_id: uuid_mod.UUID,
    ) -> bool:
        result = await self._session.execute(
            update(AgentRun)
            .where(
                AgentRun.id == run_id,
                AgentRun.status == AgentRunStatus.QUEUED.value,
            )
            .values(
                status=AgentRunStatus.FAILED.value,
                error_code=AgentRunErrorCode.ENQUEUE_FAILED.value,
            )
            .execution_options(synchronize_session=False)
        )
        return (result.rowcount or 0) == 1
