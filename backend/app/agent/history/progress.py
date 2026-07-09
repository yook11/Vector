"""Best-effort agent run progress persistence."""

from __future__ import annotations

from uuid import UUID

import structlog
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent.contract import AnswerProgressStage
from app.agent.history.types import AgentRunProgressStage, AgentRunStatus
from app.models.agent_run import AgentRun

logger = structlog.get_logger(__name__)


class AgentRunProgressWriter:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        run_id: UUID,
    ) -> None:
        self._session_factory = session_factory
        self._run_id = run_id

    async def stage_changed(self, stage: AnswerProgressStage) -> None:
        try:
            progress_stage = AgentRunProgressStage(stage).value
            async with self._session_factory() as session:
                async with session.begin():
                    await session.execute(
                        update(AgentRun)
                        .where(
                            AgentRun.id == self._run_id,
                            AgentRun.status == AgentRunStatus.RUNNING.value,
                        )
                        .values(progress_stage=progress_stage)
                        .execution_options(synchronize_session=False)
                    )
        except Exception:
            logger.warning(
                "agent_run_progress_update_failed",
                run_id=str(self._run_id),
                stage=stage,
            )
