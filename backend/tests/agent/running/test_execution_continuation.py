"""現在の run 実行を続けてよいかの判定(実DBに seed した Run に対して確かめる)。"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent.running.finish_notice import ClosedRun, ClosedRunningRun
from app.agent.running.state import (
    AttemptSuperseded,
    CanContinue,
    CannotContinue,
    DeadlineExceeded,
)
from app.agent.running.state_repository import AgentRunStateRepository
from app.agent.running.types import AgentRunStatus
from app.models.agent_run import AgentRun
from app.models.agent_user_daily_quota import AgentUserDailyQuota
from tests.agent.running._seed import create_thread_message_run
from tests.conftest import TEST_USER_ID

pytestmark = pytest.mark.integration

_USER_ID = uuid.UUID(TEST_USER_ID)
_USAGE_DATE = date(2026, 9, 2)
_DEADLINE_AT = datetime(2026, 9, 2, 12, 1, tzinfo=UTC)
_CREATED_AT = _DEADLINE_AT - timedelta(seconds=60)
_BEFORE_DEADLINE = _DEADLINE_AT - timedelta(microseconds=1)


async def _seed_running(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    status: str = "running",
    attempt_epoch: int = 1,
) -> uuid.UUID:
    async with session_factory() as session:
        session.add(
            AgentUserDailyQuota(
                user_id=_USER_ID,
                usage_date=_USAGE_DATE,
                used_count=1,
            )
        )
        _thread, _message, run = await create_thread_message_run(
            session,
            status=status,
            created_at=_CREATED_AT,
            deadline_at=_DEADLINE_AT,
            attempt_epoch=attempt_epoch,
            quota_usage_date=_USAGE_DATE,
        )
    return run.id


async def _decide(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    run_id: uuid.UUID,
    attempt_epoch: int,
    now: datetime,
) -> tuple[CanContinue | CannotContinue, list[ClosedRun]]:
    async with session_factory() as session:
        async with session.begin():
            repository = AgentRunStateRepository(session)
            result = await repository.check_continuation(
                run_id,
                attempt_epoch=attempt_epoch,
                now=now,
            )
    return result, repository.closed_runs


async def _load(
    session_factory: async_sessionmaker[AsyncSession],
    run_id: uuid.UUID,
) -> tuple[str, int, int]:
    async with session_factory() as session:
        run = await session.get(AgentRun, run_id)
        assert run is not None
        used_count = await session.scalar(
            select(AgentUserDailyQuota.used_count).where(
                AgentUserDailyQuota.user_id == _USER_ID,
                AgentUserDailyQuota.usage_date == _USAGE_DATE,
            )
        )
    assert used_count is not None
    return run.status, run.attempt_epoch, int(used_count)


@pytest.mark.asyncio
async def test_current_attempt_before_deadline_continues_without_write(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """現在の世代の継続確認は、受理期限の直前なら続けてよいと返し、Runを書き換えない。"""
    run_id = await _seed_running(session_factory)
    result = await _decide(
        session_factory,
        run_id=run_id,
        attempt_epoch=1,
        now=_BEFORE_DEADLINE,
    )
    assert result == (CanContinue(), [])
    assert await _load(session_factory, run_id) == ("running", 1, 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("now", [_DEADLINE_AT, _DEADLINE_AT + timedelta(seconds=1)])
async def test_current_attempt_at_or_after_deadline_stops_and_expires(
    session_factory: async_sessionmaker[AsyncSession],
    now: datetime,
) -> None:
    """現在の世代の継続確認は、受理期限ちょうど・超過でRunを期限切れにして終え、その世代へ終了を知らせる。"""
    run_id = await _seed_running(session_factory)
    result = await _decide(
        session_factory,
        run_id=run_id,
        attempt_epoch=1,
        now=now,
    )
    assert result == (
        DeadlineExceeded(),
        [
            ClosedRunningRun(
                run_id=run_id,
                attempt_epoch=1,
                status=AgentRunStatus.DEADLINE_EXCEEDED,
                error_code=None,
            )
        ],
    )
    assert await _load(session_factory, run_id) == ("deadline_exceeded", 1, 1)


@pytest.mark.asyncio
async def test_wrong_epoch_or_non_running_stops_without_write(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """古い世代の継続確認は、受理期限ちょうどでも期限切れにせず別の試行が担当していると返す。"""
    run_id = await _seed_running(session_factory, attempt_epoch=2)
    result = await _decide(
        session_factory,
        run_id=run_id,
        attempt_epoch=1,
        now=_DEADLINE_AT,
    )
    assert result == (AttemptSuperseded(), [])
    assert await _load(session_factory, run_id) == ("running", 2, 1)


@pytest.mark.asyncio
async def test_already_expired_run_stops_without_rewrite(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """既に期限切れで終わったRunの継続確認は、書き換えも通知もせずに期限切れと返す。"""
    run_id = await _seed_running(session_factory, status="deadline_exceeded")
    result = await _decide(
        session_factory,
        run_id=run_id,
        attempt_epoch=1,
        now=_DEADLINE_AT,
    )
    assert result == (DeadlineExceeded(), [])
    assert await _load(session_factory, run_id) == ("deadline_exceeded", 1, 1)
