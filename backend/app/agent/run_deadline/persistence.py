"""DB時刻による期限超過の確定と回収。"""

from __future__ import annotations

import uuid as uuid_mod
from datetime import date, datetime

from sqlalchemy import and_, func, literal, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from app.agent.answering import timing as answer_timing
from app.agent.daily_quota.persistence import release_daily_quotas
from app.agent.run_deadline.contracts import (
    DeadlineExceededRunningRun,
    DeadlineRunSweepResult,
)
from app.agent.runs.types import AgentRunStatus
from app.models.agent_run import AgentRun
from app.models.agent_thread import AgentThread


async def database_now(session: AsyncSession, injected: datetime | None) -> datetime:
    expression = literal(injected) if injected is not None else func.clock_timestamp()
    value = await session.scalar(select(expression))
    if not isinstance(value, datetime):
        raise RuntimeError("database clock did not return a datetime")
    return value


def _is_recovery_due(now: ColumnElement[datetime]) -> ColumnElement[bool]:
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
    *,
    now: datetime | None = None,
) -> DeadlineRunSweepResult:
    candidate_time = literal(now) if now is not None else func.clock_timestamp()
    candidate_rows = (
        (
            await session.execute(
                select(
                    AgentRun.id,
                    AgentRun.status,
                    AgentRun.attempt_epoch,
                    AgentRun.quota_usage_date,
                    AgentThread.user_id,
                )
                .join(AgentThread, AgentRun.thread_id == AgentThread.id)
                .where(_is_recovery_due(candidate_time))
                .order_by(AgentRun.id)
                .with_for_update()
            )
        )
        .tuples()
        .all()
    )
    if not candidate_rows:
        return DeadlineRunSweepResult(
            queued_terminal_count=0,
            queued_quota_released_count=0,
            queued_quota_not_eligible_count=0,
            queued_quota_inconsistent_count=0,
            running_terminal_runs=(),
            running_quota_reservation_count=0,
        )

    # lock待機中に期限を越えるため、更新判断には取得後のDB時刻を使う。
    now = await database_now(session, now)
    candidate_ids = [row[0] for row in candidate_rows]
    candidate_by_id = {row[0]: row for row in candidate_rows}
    updated_rows = (
        (
            await session.execute(
                update(AgentRun)
                .where(
                    AgentRun.id.in_(candidate_ids),
                    _is_recovery_due(literal(now)),
                )
                .values(
                    status=AgentRunStatus.DEADLINE_EXCEEDED.value,
                    assistant_message_id=None,
                    error_code=None,
                )
                .returning(
                    AgentRun.id,
                    AgentRun.status,
                    AgentRun.attempt_epoch,
                    AgentRun.quota_usage_date,
                )
                .execution_options(synchronize_session=False)
            )
        )
        .tuples()
        .all()
    )
    updated_ids = {row[0] for row in updated_rows}
    if updated_ids != set(candidate_ids):
        raise RuntimeError("deadline run sweep lost a locked candidate")

    queued_rows = [
        row
        for row in updated_rows
        if candidate_by_id[row[0]][1] == AgentRunStatus.QUEUED.value
    ]
    queued_release_groups: dict[tuple[uuid_mod.UUID, date], int] = {}
    for run_id, _status, _attempt_epoch, quota_usage_date in queued_rows:
        if quota_usage_date is None:
            continue
        user_id = candidate_by_id[run_id][4]
        group = (user_id, quota_usage_date)
        queued_release_groups[group] = queued_release_groups.get(group, 0) + 1

    released_groups = await release_daily_quotas(
        session,
        reservations=queued_release_groups,
    )

    queued_quota_released_count = sum(
        count
        for group, count in queued_release_groups.items()
        if group in released_groups
    )
    queued_quota_inconsistent_count = sum(
        count
        for group, count in queued_release_groups.items()
        if group not in released_groups
    )
    running_rows = [
        row
        for row in updated_rows
        if candidate_by_id[row[0]][1] == AgentRunStatus.RUNNING.value
    ]
    return DeadlineRunSweepResult(
        queued_terminal_count=len(queued_rows),
        queued_quota_released_count=queued_quota_released_count,
        queued_quota_not_eligible_count=sum(
            quota_usage_date is None
            for _run_id, _status, _attempt_epoch, quota_usage_date in queued_rows
        ),
        queued_quota_inconsistent_count=queued_quota_inconsistent_count,
        running_terminal_runs=tuple(
            DeadlineExceededRunningRun(run_id=run_id, attempt_epoch=attempt_epoch)
            for run_id, _status, attempt_epoch, _quota_usage_date in running_rows
            if attempt_epoch > 0
        ),
        running_quota_reservation_count=sum(
            quota_usage_date is not None
            for _run_id, _status, _attempt_epoch, quota_usage_date in running_rows
        ),
    )
