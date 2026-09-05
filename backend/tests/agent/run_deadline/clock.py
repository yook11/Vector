"""期限回収テストで時刻を固定する経路。"""

from datetime import datetime
from uuid import UUID

from sqlalchemy import literal
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.run_deadline.contracts import DeadlineRunSweepResult
from app.agent.run_deadline.persistence import _recover_deadline_exceeded_runs


async def sweep_deadline_exceeded_runs_at(
    session: AsyncSession, *, at: datetime
) -> DeadlineRunSweepResult:
    return await _recover_deadline_exceeded_runs(
        session,
        thread_id=None,
        clock=literal(at),
    )


async def sweep_deadline_exceeded_runs_for_thread_at(
    session: AsyncSession, *, thread_id: UUID, at: datetime
) -> DeadlineRunSweepResult:
    return await _recover_deadline_exceeded_runs(
        session,
        thread_id=thread_id,
        clock=literal(at),
    )
