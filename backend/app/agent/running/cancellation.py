from __future__ import annotations

import uuid as uuid_mod
from dataclasses import dataclass
from enum import StrEnum

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.running.daily_quota.release import (
    DailyQuotaReleaseOutcome,
    release_daily_quota,
)
from app.agent.runs.types import AgentRunErrorCode, AgentRunStatus
from app.models.agent_run import AgentRun
from app.models.agent_thread import AgentThread


class RunCancellationFailureReason(StrEnum):
    RUN_NOT_FOUND = "run_not_found"
    ALREADY_FAILED = "already_failed"
    ALREADY_COMPLETED = "already_completed"
    ALREADY_POLICY_BLOCKED = "already_policy_blocked"
    ALREADY_DEADLINE_EXCEEDED = "already_deadline_exceeded"


@dataclass(frozen=True, slots=True)
class RunCancellationFailure:
    reason: RunCancellationFailureReason


@dataclass(frozen=True, slots=True)
class RunCancellationSuccess:
    quota_release_outcome: DailyQuotaReleaseOutcome
    running_attempt_epoch: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.quota_release_outcome, DailyQuotaReleaseOutcome):
            raise ValueError("cancelled run requires a quota release outcome")
        if self.running_attempt_epoch is not None:
            if (
                not isinstance(self.running_attempt_epoch, int)
                or isinstance(self.running_attempt_epoch, bool)
                or self.running_attempt_epoch < 1
            ):
                raise ValueError("running cancel requires a positive attempt epoch")


class AgentRunCancellationRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def cancel_run_for_user(
        self,
        *,
        run_id: uuid_mod.UUID,
        user_id: uuid_mod.UUID,
    ) -> RunCancellationSuccess | RunCancellationFailure:
        owned_thread_ids = select(AgentThread.id).where(AgentThread.user_id == user_id)
        queued_result = await self._session.execute(
            update(AgentRun)
            .where(
                AgentRun.id == run_id,
                AgentRun.status == AgentRunStatus.QUEUED.value,
                AgentRun.thread_id.in_(owned_thread_ids),
            )
            .values(
                status=AgentRunStatus.FAILED.value,
                error_code=AgentRunErrorCode.CANCELLED.value,
            )
            .returning(AgentRun.quota_usage_date)
            .execution_options(synchronize_session=False)
        )
        queued_row = queued_result.one_or_none()
        if queued_row is not None:
            quota_release_outcome = await release_daily_quota(
                self._session,
                user_id=user_id,
                usage_date=queued_row.quota_usage_date,
            )
            return RunCancellationSuccess(
                quota_release_outcome=quota_release_outcome,
            )

        running_result = await self._session.execute(
            update(AgentRun)
            .where(
                AgentRun.id == run_id,
                AgentRun.status == AgentRunStatus.RUNNING.value,
                AgentRun.thread_id.in_(owned_thread_ids),
            )
            .values(
                status=AgentRunStatus.FAILED.value,
                error_code=AgentRunErrorCode.CANCELLED.value,
            )
            .returning(AgentRun.attempt_epoch)
            .execution_options(synchronize_session=False)
        )
        running_attempt_epoch = running_result.scalar_one_or_none()
        if running_attempt_epoch is not None:
            return RunCancellationSuccess(
                running_attempt_epoch=running_attempt_epoch,
                quota_release_outcome=DailyQuotaReleaseOutcome.NOT_ELIGIBLE,
            )

        terminal_status = (
            await self._session.execute(
                select(AgentRun.status).where(
                    AgentRun.id == run_id,
                    AgentRun.thread_id.in_(owned_thread_ids),
                )
            )
        ).scalar_one_or_none()
        if terminal_status is None:
            return RunCancellationFailure(RunCancellationFailureReason.RUN_NOT_FOUND)
        if terminal_status == AgentRunStatus.COMPLETED:
            return RunCancellationFailure(
                RunCancellationFailureReason.ALREADY_COMPLETED
            )
        if terminal_status == AgentRunStatus.FAILED:
            return RunCancellationFailure(RunCancellationFailureReason.ALREADY_FAILED)
        if terminal_status == AgentRunStatus.POLICY_BLOCKED:
            return RunCancellationFailure(
                RunCancellationFailureReason.ALREADY_POLICY_BLOCKED
            )
        if terminal_status == AgentRunStatus.DEADLINE_EXCEEDED:
            return RunCancellationFailure(
                RunCancellationFailureReason.ALREADY_DEADLINE_EXCEEDED
            )
        raise RuntimeError("cancel run encountered an unexpected state")
