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
from app.agent.running.state import RunState
from app.agent.running.types import AgentRunStatus, RunAttempt
from app.models.agent_run import AgentRun
from app.models.agent_thread import AgentThread


@dataclass(frozen=True, slots=True)
class _DeadlineRecoveryCandidate:
    run_id: UUID
    user_id: UUID
    quota_usage_date: date | None
    state: RunState


@dataclass(frozen=True, slots=True)
class DeadlineRunSweepResult:
    queued_terminal_count: int
    queued_quota_released_count: int
    queued_quota_not_eligible_count: int
    queued_quota_inconsistent_count: int
    running_terminal_runs: tuple[RunAttempt, ...]
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
        if not all(isinstance(run, RunAttempt) for run in self.running_terminal_runs):
            raise ValueError("running terminal runs must be run attempts")

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
    """候補を絞るだけの条件で、ロック後の確定はRunStateが判断する。"""
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
    user_id: uuid_mod.UUID,
) -> DeadlineRunSweepResult:
    return await _recover_deadline_exceeded_runs(
        session,
        thread_id=thread_id,
        user_id=user_id,
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
    user_id: uuid_mod.UUID | None = None,
    run_id: UUID | None = None,
) -> DeadlineRunSweepResult:
    candidates = await _lock_runs_for_deadline_recovery(
        session, thread_id=thread_id, user_id=user_id, run_id=run_id, clock=clock
    )
    if not candidates:
        return DeadlineRunSweepResult.empty()

    # lock待機中に期限を越えるため、更新判断には取得後のDB時刻を使う。
    now = await _read_database_time(session, clock)
    await _finalize_deadline_exceeded_runs(session, candidates=candidates, now=now)
    quota_result = await release_daily_quotas(
        session,
        reservations=tuple(
            DailyQuotaReleaseReservation(
                user_id=candidate.user_id,
                usage_date=candidate.quota_usage_date,
            )
            for candidate in candidates
            if candidate.state.status is AgentRunStatus.QUEUED
        ),
    )
    return _build_deadline_sweep_result(candidates, quota_result)


async def _lock_runs_for_deadline_recovery(
    session: AsyncSession,
    *,
    thread_id: UUID | None,
    user_id: UUID | None,
    run_id: UUID | None,
    clock: ColumnElement[datetime],
) -> tuple[_DeadlineRecoveryCandidate, ...]:
    query = (
        select(
            AgentRun.id,
            AgentRun.status,
            AgentRun.attempt_epoch,
            AgentRun.deadline_at,
            AgentRun.answer_started_at,
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
    if user_id is not None:
        query = query.where(AgentThread.user_id == user_id)
    if run_id is not None:
        query = query.where(AgentRun.id == run_id)
    rows = (await session.execute(query)).tuples().all()
    return tuple(
        _DeadlineRecoveryCandidate(
            run_id=candidate_id,
            user_id=user_id,
            quota_usage_date=quota_usage_date,
            state=RunState(
                status=AgentRunStatus(status),
                attempt_epoch=attempt_epoch,
                deadline_at=deadline_at,
                answer_started_at=answer_started_at,
            ),
        )
        for (
            candidate_id,
            status,
            attempt_epoch,
            deadline_at,
            answer_started_at,
            quota_usage_date,
            user_id,
        ) in rows
    )


async def _finalize_deadline_exceeded_runs(
    session: AsyncSession,
    *,
    candidates: tuple[_DeadlineRecoveryCandidate, ...],
    now: datetime,
) -> None:
    if not all(
        candidate.state.is_due_for_deadline_recovery(now=now)
        for candidate in candidates
    ):
        raise RuntimeError("deadline run sweep lost a locked candidate")
    await session.execute(
        update(AgentRun)
        .where(AgentRun.id.in_([candidate.run_id for candidate in candidates]))
        # 回答とerror_codeは制約で既にNULLのため書かず、
        # APIロールにこの2列の更新権限を求めない。
        .values(status=AgentRunStatus.DEADLINE_EXCEEDED.value)
        .execution_options(synchronize_session=False)
    )


def _build_deadline_sweep_result(
    candidates: tuple[_DeadlineRecoveryCandidate, ...],
    quota_result: DailyQuotaBatchReleaseResult,
) -> DeadlineRunSweepResult:
    running = [
        candidate
        for candidate in candidates
        if candidate.state.status is AgentRunStatus.RUNNING
    ]
    return DeadlineRunSweepResult(
        queued_terminal_count=sum(
            candidate.state.status is AgentRunStatus.QUEUED for candidate in candidates
        ),
        queued_quota_released_count=quota_result.released_count,
        queued_quota_not_eligible_count=quota_result.not_eligible_count,
        queued_quota_inconsistent_count=quota_result.inconsistent_count,
        running_terminal_runs=tuple(
            RunAttempt(
                run_id=candidate.run_id,
                epoch=candidate.state.attempt_epoch,
            )
            for candidate in running
            if candidate.state.attempt_epoch > 0
        ),
        running_quota_reservation_count=sum(
            candidate.quota_usage_date is not None for candidate in running
        ),
    )
