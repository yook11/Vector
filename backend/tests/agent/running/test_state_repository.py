"""AgentRunStateRepository が記録系の操作で期限と利用枠を扱う規則(実DB)。"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent.running.daily_quota.release import DailyQuotaReleaseOutcome
from app.agent.running.state import RunFinished
from app.agent.running.state_repository import AgentRunStateRepository
from app.agent.running.types import AgentRunErrorCode, AgentRunStatus
from app.models.agent_run import AgentRun
from app.models.agent_user_daily_quota import AgentUserDailyQuota
from tests.agent.running._seed import create_thread_message_run
from tests.conftest import TEST_USER_ID

_USER_ID = uuid.UUID(TEST_USER_ID)
_USAGE_DATE = date(2026, 11, 3)
_NOW = datetime(2026, 11, 3, 3, 0, tzinfo=UTC)


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
async def test_enqueue_failure_releases_quota_reserved_at_admission(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """キュー投入に失敗したqueuedのRunはenqueue_failedで終わり、受付で予約した利用枠が戻る。"""
    await _seed_quota_counter(session_factory, used_count=1)
    async with session_factory() as session:
        _thread, _message, run = await create_thread_message_run(
            session,
            status="queued",
            deadline_at=_NOW + timedelta(seconds=60),
            quota_usage_date=_USAGE_DATE,
        )

    async with session_factory() as session:
        async with session.begin():
            result = await AgentRunStateRepository(session).record_enqueue_failure(
                run.id, now=_NOW
            )

    assert (
        result,
        await _read_counter(session_factory),
        await _read_run(session_factory, run.id),
    ) == (
        RunFinished(
            status=AgentRunStatus.FAILED,
            error_code=AgentRunErrorCode.ENQUEUE_FAILED,
            quota_release_outcome=DailyQuotaReleaseOutcome.RELEASED,
            running_attempt_epoch=None,
        ),
        0,
        ("failed", "enqueue_failed", 0),
    )


@pytest.mark.asyncio
async def test_cancel_after_deadline_expires_queued_run_and_releases_quota(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """受理期限を過ぎたqueuedのRunをキャンセルすると、期限切れで終わり利用枠が戻る。"""
    await _seed_quota_counter(session_factory, used_count=1)
    async with session_factory() as session:
        _thread, _message, run = await create_thread_message_run(
            session,
            status="queued",
            deadline_at=_NOW - timedelta(seconds=1),
            quota_usage_date=_USAGE_DATE,
        )

    async with session_factory() as session:
        async with session.begin():
            result = await AgentRunStateRepository(session).cancel_for_user(
                run.id, user_id=_USER_ID, now=_NOW
            )

    assert (
        result,
        await _read_counter(session_factory),
        await _read_run(session_factory, run.id),
    ) == (
        RunFinished(
            status=AgentRunStatus.DEADLINE_EXCEEDED,
            error_code=None,
            quota_release_outcome=DailyQuotaReleaseOutcome.RELEASED,
            running_attempt_epoch=None,
        ),
        0,
        ("deadline_exceeded", None, 0),
    )


@pytest.mark.asyncio
async def test_cancel_after_deadline_expires_running_run_for_its_attempt(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """回答前に受理期限を過ぎたrunningのRunをキャンセルすると、その世代のまま期限切れで終わる。"""
    async with session_factory() as session:
        _thread, _message, run = await create_thread_message_run(
            session,
            status="running",
            attempt_epoch=3,
            deadline_at=_NOW - timedelta(seconds=1),
        )

    async with session_factory() as session:
        async with session.begin():
            result = await AgentRunStateRepository(session).cancel_for_user(
                run.id, user_id=_USER_ID, now=_NOW
            )

    assert (result, await _read_run(session_factory, run.id)) == (
        RunFinished(
            status=AgentRunStatus.DEADLINE_EXCEEDED,
            error_code=None,
            quota_release_outcome=DailyQuotaReleaseOutcome.NOT_ELIGIBLE,
            running_attempt_epoch=3,
        ),
        ("deadline_exceeded", None, 3),
    )


@pytest.mark.asyncio
async def test_failure_after_recovery_deadline_expires_answering_run(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """回答開始から45秒を過ぎた回答中のRunの失敗記録は、失敗ではなく期限切れで終わる。"""
    async with session_factory() as session:
        _thread, _message, run = await create_thread_message_run(
            session,
            status="running",
            attempt_epoch=2,
            deadline_at=_NOW - timedelta(seconds=30),
            answer_started_at=_NOW - timedelta(seconds=45),
        )

    async with session_factory() as session:
        async with session.begin():
            result = await AgentRunStateRepository(session).record_failure(
                run.id,
                attempt_epoch=2,
                error_code=AgentRunErrorCode.GENERATION_UNAVAILABLE,
                now=_NOW,
            )

    assert (result, await _read_run(session_factory, run.id)) == (
        RunFinished(
            status=AgentRunStatus.DEADLINE_EXCEEDED,
            error_code=None,
            quota_release_outcome=DailyQuotaReleaseOutcome.NOT_ELIGIBLE,
            running_attempt_epoch=2,
        ),
        ("deadline_exceeded", None, 2),
    )


@pytest.mark.asyncio
async def test_start_after_deadline_reports_redelivered_running_attempt(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """受理期限後に再配信されたrunningのRunは期限切れで終わり、終了通知の送り先として元の世代を返す。"""
    async with session_factory() as session:
        _thread, _message, run = await create_thread_message_run(
            session,
            status="running",
            attempt_epoch=3,
            deadline_at=_NOW - timedelta(seconds=1),
        )

    async with session_factory() as session:
        async with session.begin():
            result = await AgentRunStateRepository(session).start_attempt(
                run.id, now=_NOW
            )

    assert (result, await _read_run(session_factory, run.id)) == (
        RunFinished(
            status=AgentRunStatus.DEADLINE_EXCEEDED,
            error_code=None,
            quota_release_outcome=DailyQuotaReleaseOutcome.NOT_ELIGIBLE,
            running_attempt_epoch=3,
        ),
        ("deadline_exceeded", None, 3),
    )
