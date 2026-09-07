"""DB時刻による期限超過の確定と回収。"""

from __future__ import annotations

import uuid as uuid_mod
from dataclasses import dataclass
from datetime import date, datetime
from uuid import UUID

from sqlalchemy import and_, func, literal, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from app.agent.answering import timing as answer_timing
from app.agent.running.daily_quota.release import (
    DailyQuotaBatchReleaseResult,
    DailyQuotaReleaseReservation,
    release_daily_quotas,
)
from app.agent.runs.types import AgentRunStatus
from app.models.agent_run import AgentRun
from app.models.agent_thread import AgentThread


@dataclass(frozen=True, slots=True)
class LockedRunForDeadlineRecovery:
    """期限回収のためにロックした時点のrun情報を保持する。"""

    run_id: UUID
    status: AgentRunStatus
    attempt_epoch: int
    user_id: UUID
    quota_usage_date: date | None


@dataclass(frozen=True, slots=True)
class DeadlineExceededRunningRun:
    run_id: UUID
    attempt_epoch: int

    def __post_init__(self) -> None:
        if (
            not isinstance(self.attempt_epoch, int)
            or isinstance(self.attempt_epoch, bool)
            or self.attempt_epoch < 1
        ):
            raise ValueError(
                "deadline-exceeded running run requires a positive attempt epoch"
            )


@dataclass(frozen=True, slots=True)
class DeadlineRunSweepResult:
    queued_terminal_count: int
    queued_quota_released_count: int
    queued_quota_not_eligible_count: int
    queued_quota_inconsistent_count: int
    running_terminal_runs: tuple[DeadlineExceededRunningRun, ...]
    running_quota_reservation_count: int

    def __post_init__(self) -> None:
        counts = (
            self.queued_terminal_count,
            self.queued_quota_released_count,
            self.queued_quota_not_eligible_count,
            self.queued_quota_inconsistent_count,
            self.running_quota_reservation_count,
        )
        if any(
            not isinstance(count, int) or isinstance(count, bool) or count < 0
            for count in counts
        ):
            raise ValueError("deadline run sweep counts must be non-negative integers")
        if (
            self.queued_quota_released_count
            + self.queued_quota_not_eligible_count
            + self.queued_quota_inconsistent_count
            != self.queued_terminal_count
        ):
            raise ValueError("queued quota outcomes must equal terminal count")
        if not all(
            isinstance(run, DeadlineExceededRunningRun)
            for run in self.running_terminal_runs
        ):
            raise ValueError(
                "running terminal runs must be deadline-exceeded running runs"
            )

    @classmethod
    def empty(cls) -> DeadlineRunSweepResult:
        return cls(
            queued_terminal_count=0,
            queued_quota_released_count=0,
            queued_quota_not_eligible_count=0,
            queued_quota_inconsistent_count=0,
            running_terminal_runs=(),
            running_quota_reservation_count=0,
        )

    @property
    def total_count(self) -> int:
        return self.queued_terminal_count + len(self.running_terminal_runs)


async def _read_database_time(
    session: AsyncSession, expression: ColumnElement[datetime]
) -> datetime:
    value = await session.scalar(select(expression))
    if not isinstance(value, datetime):
        raise RuntimeError("database clock did not return a datetime")
    return value


async def database_now(session: AsyncSession, injected: datetime | None) -> datetime:
    expression = literal(injected) if injected is not None else func.clock_timestamp()
    return await _read_database_time(session, expression)


def _has_reached_recovery_deadline(now: ColumnElement[datetime]) -> ColumnElement[bool]:
    return or_(
        and_(
            AgentRun.status == AgentRunStatus.QUEUED.value,
            AgentRun.deadline_at <= now,
        ),
        and_(
            AgentRun.status == AgentRunStatus.RUNNING.value,
            AgentRun.answer_started_at.is_(None),
            AgentRun.deadline_at <= now,
        ),
        and_(
            AgentRun.status == AgentRunStatus.RUNNING.value,
            AgentRun.answer_started_at.is_not(None),
            AgentRun.answer_started_at
            + answer_timing.answer_generation_recovery_window()
            <= now,
        ),
    )


async def expire_run(
    session: AsyncSession,
    *,
    run_id: uuid_mod.UUID,
    expected_status: AgentRunStatus,
    expected_attempt_epoch: int,
    now: datetime,
) -> bool:
    """呼び出し元がRunをロックし、取得後のDB時刻で期限切れを確定する。"""
    result = await session.execute(
        update(AgentRun)
        .where(
            AgentRun.id == run_id,
            AgentRun.status == expected_status.value,
            AgentRun.attempt_epoch == expected_attempt_epoch,
            AgentRun.deadline_at <= now,
        )
        .values(
            status=AgentRunStatus.DEADLINE_EXCEEDED.value,
            assistant_message_id=None,
            error_code=None,
        )
        .execution_options(synchronize_session=False)
    )
    return (result.rowcount or 0) == 1


async def sweep_deadline_exceeded_runs(
    session: AsyncSession,
) -> DeadlineRunSweepResult:
    return await _recover_deadline_exceeded_runs(
        session,
        thread_id=None,
        clock=func.clock_timestamp(),
    )


async def sweep_deadline_exceeded_runs_for_thread(
    session: AsyncSession,
    *,
    thread_id: uuid_mod.UUID,
) -> DeadlineRunSweepResult:
    return await _recover_deadline_exceeded_runs(
        session,
        thread_id=thread_id,
        clock=func.clock_timestamp(),
    )


async def check_agent_run_deadline(
    session: AsyncSession, *, run_id: UUID
) -> DeadlineRunSweepResult:
    return await _recover_deadline_exceeded_runs(
        session, thread_id=None, run_id=run_id, clock=func.clock_timestamp()
    )


async def _recover_deadline_exceeded_runs(
    session: AsyncSession,
    *,
    thread_id: uuid_mod.UUID | None,
    clock: ColumnElement[datetime],
    run_id: UUID | None = None,
) -> DeadlineRunSweepResult:
    locked_runs = await _lock_runs_for_deadline_recovery(
        session, thread_id=thread_id, run_id=run_id, clock=clock
    )
    if not locked_runs:
        return DeadlineRunSweepResult.empty()

    # lock待機中に期限を越えるため、更新判断には取得後のDB時刻を使う。
    now = await _read_database_time(session, clock)
    await _finalize_deadline_exceeded_runs(session, locked_runs=locked_runs, now=now)
    quota_result = await release_daily_quotas(
        session,
        reservations=tuple(
            DailyQuotaReleaseReservation(
                user_id=run.user_id,
                usage_date=run.quota_usage_date,
            )
            for run in locked_runs
            if run.status == AgentRunStatus.QUEUED
        ),
    )
    return _build_deadline_sweep_result(locked_runs, quota_result)


async def _lock_runs_for_deadline_recovery(
    session: AsyncSession,
    *,
    thread_id: UUID | None,
    run_id: UUID | None,
    clock: ColumnElement[datetime],
) -> tuple[LockedRunForDeadlineRecovery, ...]:
    query = (
        select(
            AgentRun.id,
            AgentRun.status,
            AgentRun.attempt_epoch,
            AgentRun.quota_usage_date,
            AgentThread.user_id,
        )
        .join(AgentThread, AgentRun.thread_id == AgentThread.id)
        .where(_has_reached_recovery_deadline(clock))
        .order_by(AgentRun.id)
        .with_for_update()
    )
    if thread_id is not None:
        query = query.where(AgentRun.thread_id == thread_id)
    if run_id is not None:
        query = query.where(AgentRun.id == run_id)
    rows = (await session.execute(query)).tuples().all()
    return tuple(
        LockedRunForDeadlineRecovery(
            run_id=run_id,
            status=AgentRunStatus(status),
            attempt_epoch=attempt_epoch,
            user_id=user_id,
            quota_usage_date=quota_usage_date,
        )
        for run_id, status, attempt_epoch, quota_usage_date, user_id in rows
    )


async def _finalize_deadline_exceeded_runs(
    session: AsyncSession,
    *,
    locked_runs: tuple[LockedRunForDeadlineRecovery, ...],
    now: datetime,
) -> None:
    run_ids = [run.run_id for run in locked_runs]
    updated_ids = set(
        (
            await session.scalars(
                update(AgentRun)
                .where(
                    AgentRun.id.in_(run_ids),
                    _has_reached_recovery_deadline(literal(now)),
                )
                .values(
                    status=AgentRunStatus.DEADLINE_EXCEEDED.value,
                    assistant_message_id=None,
                    error_code=None,
                )
                .returning(AgentRun.id)
                .execution_options(synchronize_session=False)
            )
        ).all()
    )
    if updated_ids != set(run_ids):
        raise RuntimeError("deadline run sweep lost a locked candidate")


def _build_deadline_sweep_result(
    locked_runs: tuple[LockedRunForDeadlineRecovery, ...],
    quota_result: DailyQuotaBatchReleaseResult,
) -> DeadlineRunSweepResult:
    running_runs = [run for run in locked_runs if run.status == AgentRunStatus.RUNNING]
    return DeadlineRunSweepResult(
        queued_terminal_count=sum(
            run.status == AgentRunStatus.QUEUED for run in locked_runs
        ),
        queued_quota_released_count=quota_result.released_count,
        queued_quota_not_eligible_count=quota_result.not_eligible_count,
        queued_quota_inconsistent_count=quota_result.inconsistent_count,
        running_terminal_runs=tuple(
            DeadlineExceededRunningRun(
                run_id=run.run_id, attempt_epoch=run.attempt_epoch
            )
            for run in running_runs
            if run.attempt_epoch > 0
        ),
        running_quota_reservation_count=sum(
            run.quota_usage_date is not None for run in running_runs
        ),
    )
