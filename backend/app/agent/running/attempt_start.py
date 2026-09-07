"""実行の開始可否を判断し、現在の実行世代を確定する。"""

from __future__ import annotations

import uuid as uuid_mod
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

import structlog
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.running.daily_quota import observability as daily_quota_observability
from app.agent.running.daily_quota.release import release_daily_quota
from app.agent.running.deadline.deadline_exceeded import database_now, expire_run
from app.agent.runs.types import AgentRunStatus
from app.models.agent_run import AgentRun
from app.models.agent_thread import AgentThread

logger = structlog.get_logger(__name__)

_ACTIVE_STATUSES = (AgentRunStatus.QUEUED.value, AgentRunStatus.RUNNING.value)
_TERMINAL_STATUSES = (
    AgentRunStatus.COMPLETED.value,
    AgentRunStatus.POLICY_BLOCKED.value,
    AgentRunStatus.DEADLINE_EXCEEDED.value,
    AgentRunStatus.FAILED.value,
)


class StartRunFailureReason(StrEnum):
    RUN_NOT_FOUND = "run_not_found"
    ALREADY_FINISHED = "already_finished"
    DEADLINE_EXCEEDED = "deadline_exceeded"
    UNEXPECTED = "unexpected"


@dataclass(frozen=True, slots=True)
class StartRunFailure:
    reason: StartRunFailureReason

    def __post_init__(self) -> None:
        if not isinstance(self.reason, StartRunFailureReason):
            raise ValueError("invalid start run failure reason")


class AgentRunAttemptStartRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def start_run(
        self,
        run_id: uuid_mod.UUID,
        *,
        now: datetime | None = None,
    ) -> int | StartRunFailure:
        row = (
            await self._session.execute(
                select(
                    AgentRun,
                    AgentThread.user_id,
                )
                .join(AgentThread, AgentRun.thread_id == AgentThread.id)
                .where(AgentRun.id == run_id)
                .with_for_update(of=AgentRun)
                .execution_options(populate_existing=True)
            )
        ).one_or_none()
        if row is None:
            return StartRunFailure(StartRunFailureReason.RUN_NOT_FOUND)
        run, user_id = row
        if run.status in _TERMINAL_STATUSES:
            return StartRunFailure(StartRunFailureReason.ALREADY_FINISHED)

        if run.status not in _ACTIVE_STATUSES:
            logger.error(
                "agent_run_start_unexpected",
                run_id=str(run_id),
                observed_status=run.status,
            )
            return StartRunFailure(StartRunFailureReason.UNEXPECTED)

        now = await database_now(self._session, now)
        if now >= run.deadline_at:
            expired = await expire_run(
                self._session,
                run_id=run_id,
                expected_status=AgentRunStatus(run.status),
                expected_attempt_epoch=run.attempt_epoch,
                now=now,
            )
            if not expired:
                logger.error(
                    "agent_run_start_unexpected",
                    run_id=str(run_id),
                    cause="deadline_write",
                    observed_status=run.status,
                )
                return StartRunFailure(StartRunFailureReason.UNEXPECTED)
            if run.status == AgentRunStatus.QUEUED.value:
                outcome = await release_daily_quota(
                    self._session,
                    user_id=user_id,
                    usage_date=run.quota_usage_date,
                )
                daily_quota_observability.observe_release(
                    run_id=run_id,
                    outcome=outcome,
                )
            return StartRunFailure(StartRunFailureReason.DEADLINE_EXCEEDED)

        result = await self._session.execute(
            update(AgentRun)
            .where(
                AgentRun.id == run_id,
                AgentRun.status.in_(_ACTIVE_STATUSES),
                AgentRun.deadline_at > now,
            )
            .values(
                status=AgentRunStatus.RUNNING.value,
                attempt_epoch=AgentRun.attempt_epoch + 1,
            )
            .returning(AgentRun.attempt_epoch)
            .execution_options(synchronize_session=False)
        )
        attempt_epoch = result.scalar_one_or_none()
        if attempt_epoch is None:
            logger.error(
                "agent_run_start_unexpected",
                run_id=str(run_id),
                cause="start_write",
                observed_status=run.status,
            )
            return StartRunFailure(StartRunFailureReason.UNEXPECTED)
        return attempt_epoch
