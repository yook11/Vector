"""期限を過ぎたRunは操作が閉じず、期限の確認が閉じる(実DB、DBの時刻、通知は記録)。"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent.running.state import CannotContinue
from app.agent.running.types import AgentRunErrorCode, AgentRunStatus, RunAttempt
from app.models.agent_run import AgentRun
from app.models.agent_user_daily_quota import AgentUserDailyQuota
from tests.agent.running._seed import create_thread_message_run
from tests.agent.running._state_service import (
    ClosedRunningRun,
    ClosedRunNotices,
    state_service,
)
from tests.conftest import TEST_USER_ID

_USER_ID = uuid.UUID(TEST_USER_ID)
_USAGE_DATE = date(2026, 11, 3)


async def _seed_quota_counter(
    session_factory: async_sessionmaker[AsyncSession], *, used_count: int
) -> None:
    async with session_factory() as session:
        session.add(
            AgentUserDailyQuota(
                user_id=_USER_ID, usage_date=_USAGE_DATE, used_count=used_count
            )
        )
        await session.commit()


async def _read_counter(session_factory: async_sessionmaker[AsyncSession]) -> int:
    async with session_factory() as session:
        used_count = await session.scalar(
            select(AgentUserDailyQuota.used_count).where(
                AgentUserDailyQuota.user_id == _USER_ID,
                AgentUserDailyQuota.usage_date == _USAGE_DATE,
            )
        )
    assert used_count is not None
    return used_count


async def _read_run(
    session_factory: async_sessionmaker[AsyncSession], run_id: uuid.UUID
) -> tuple[str, str | None, int]:
    async with session_factory() as session:
        row = (
            await session.execute(
                select(
                    AgentRun.status, AgentRun.error_code, AgentRun.attempt_epoch
                ).where(AgentRun.id == run_id)
            )
        ).one()
    return row.status, row.error_code, row.attempt_epoch


@pytest.mark.asyncio
async def test_expired_queued_run_is_left_by_cancel_and_closed_by_deadline_check(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """受理期限を過ぎた待機中のRunは、取り消しでは変わらず、期限の確認が期限切れで閉じて利用枠を返す。"""
    await _seed_quota_counter(session_factory, used_count=1)
    async with session_factory() as session:
        _thread, _message, run = await create_thread_message_run(
            session,
            status="queued",
            deadline_at=datetime.now(UTC) - timedelta(seconds=1),
            quota_usage_date=_USAGE_DATE,
        )
    notices = ClosedRunNotices()
    service = state_service(session_factory, notices=notices)

    cancelled = await service.cancel_for_user(run.id, user_id=_USER_ID)
    after_cancel = (
        await _read_run(session_factory, run.id),
        await _read_counter(session_factory),
    )
    await service.end_runs_past_deadline(run_id=run.id)

    assert (
        cancelled,
        after_cancel,
        await _read_run(session_factory, run.id),
        await _read_counter(session_factory),
    ) == (
        CannotContinue(),
        (("queued", None, 0), 1),
        ("deadline_exceeded", None, 0),
        0,
    )


@pytest.mark.asyncio
async def test_expired_running_run_is_left_by_failure_and_closed_by_deadline_check(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """受理期限を過ぎた回答前のRunは、失敗の記録では変わらず、期限の確認が期限切れで閉じてその世代へ知らせる。"""
    async with session_factory() as session:
        _thread, _message, run = await create_thread_message_run(
            session,
            status="running",
            attempt_epoch=3,
            deadline_at=datetime.now(UTC) - timedelta(seconds=1),
        )
    notices = ClosedRunNotices()
    service = state_service(session_factory, notices=notices)

    recorded = await service.end_run_after_attempt_failure(
        RunAttempt(run_id=run.id, epoch=3),
        error_code=AgentRunErrorCode.GENERATION_UNAVAILABLE,
    )
    after_failure = (await _read_run(session_factory, run.id), notices.closed_runs[:])
    await service.end_runs_past_deadline(run_id=run.id)

    assert (
        recorded,
        after_failure,
        notices.closed_runs,
        await _read_run(session_factory, run.id),
    ) == (
        CannotContinue(),
        (("running", None, 3), []),
        [
            ClosedRunningRun(
                attempt=RunAttempt(run_id=run.id, epoch=3),
                status=AgentRunStatus.DEADLINE_EXCEEDED,
                error_code=None,
            )
        ],
        ("deadline_exceeded", None, 3),
    )
