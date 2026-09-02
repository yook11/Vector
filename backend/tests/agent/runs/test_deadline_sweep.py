"""queued/runningのdeadline sweepにおける永続状態契約。"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from uuid import UUID

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent.runs.repository import AgentRunRepository
from app.models.agent_message import AgentMessage
from app.models.agent_run import AgentRun
from app.models.agent_thread import AgentThread
from app.models.agent_user_daily_quota import AgentUserDailyQuota
from tests.conftest import TEST_USER_ID

pytestmark = pytest.mark.integration

_USER_ID = UUID(TEST_USER_ID)


async def _seed_run(
    session: AsyncSession,
    *,
    status: str,
    created_at: datetime,
    deadline_at: datetime | None = None,
    attempt_epoch: int = 0,
    quota_usage_date: date | None = None,
) -> AgentRun:
    thread = AgentThread(user_id=_USER_ID, title="deadline sweep")
    session.add(thread)
    await session.flush()
    message = AgentMessage(
        thread_id=thread.id,
        seq=1,
        role="user",
        content="deadline sweep question",
        missing_aspects=[],
    )
    session.add(message)
    await session.flush()

    assistant_message_id = None
    if status == "completed":
        assistant = AgentMessage(
            thread_id=thread.id,
            seq=2,
            role="assistant",
            content="completed answer",
            missing_aspects=[],
        )
        session.add(assistant)
        await session.flush()
        assistant_message_id = assistant.id

    run = AgentRun(
        thread_id=thread.id,
        user_message_id=message.id,
        assistant_message_id=assistant_message_id,
        status=status,
        created_at=created_at,
        deadline_at=(
            deadline_at
            if deadline_at is not None
            else created_at + timedelta(seconds=60)
        ),
        attempt_epoch=attempt_epoch,
        error_code="internal_error" if status == "failed" else None,
        quota_usage_date=quota_usage_date,
    )
    session.add(run)
    await session.flush()
    return run


async def _persisted_run(
    session_factory: async_sessionmaker[AsyncSession], run_id: UUID
) -> AgentRun:
    async with session_factory() as session:
        run = await session.get(AgentRun, run_id)
    assert run is not None
    return run


@pytest.mark.asyncio
@pytest.mark.parametrize("initial_status", ["queued", "running"])
async def test_sweep_leaves_run_unchanged_before_deadline(
    session_factory: async_sessionmaker[AsyncSession],
    initial_status: str,
) -> None:
    """期限直前はqueued/runningの状態を変更しない。"""
    created_at = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)
    deadline_at = datetime(2026, 9, 2, 12, 1, tzinfo=UTC)
    now = deadline_at - timedelta(microseconds=1)
    async with session_factory() as session:
        run = await _seed_run(
            session,
            status=initial_status,
            created_at=created_at,
            deadline_at=deadline_at,
            attempt_epoch=1 if initial_status == "running" else 0,
        )
        await session.commit()

        await AgentRunRepository(session).sweep_deadline_exceeded_runs(now=now)
        await session.commit()

    async with session_factory() as observer:
        persisted = (
            await observer.execute(select(AgentRun.status).where(AgentRun.id == run.id))
        ).one()

    assert persisted.status == initial_status


@pytest.mark.asyncio
@pytest.mark.parametrize("initial_status", ["queued", "running"])
@pytest.mark.parametrize(
    "now",
    [
        pytest.param(datetime(2026, 9, 2, 12, 1, tzinfo=UTC), id="at-deadline"),
        pytest.param(
            datetime(2026, 9, 2, 12, 1, 0, 1, tzinfo=UTC), id="after-deadline"
        ),
    ],
)
async def test_sweep_marks_run_deadline_exceeded_at_or_after_deadline(
    session_factory: async_sessionmaker[AsyncSession],
    initial_status: str,
    now: datetime,
) -> None:
    """期限ちょうど・超過ではqueued/runningを時間切れにする。"""
    created_at = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)
    deadline_at = datetime(2026, 9, 2, 12, 1, tzinfo=UTC)
    async with session_factory() as session:
        run = await _seed_run(
            session,
            status=initial_status,
            created_at=created_at,
            deadline_at=deadline_at,
            attempt_epoch=1 if initial_status == "running" else 0,
        )
        await session.commit()

        await AgentRunRepository(session).sweep_deadline_exceeded_runs(now=now)
        await session.commit()

    async with session_factory() as observer:
        persisted = (
            await observer.execute(select(AgentRun.status).where(AgentRun.id == run.id))
        ).one()

    assert persisted.status == "deadline_exceeded"


@pytest.mark.asyncio
async def test_sweep_releases_queued_quota_once_without_running_refund(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """queuedの予約だけを返却し、再実行してもrunningの予約は減らさない。"""
    now = datetime(2026, 7, 22, 12, 0, tzinfo=UTC)
    deadline_at = now - timedelta(microseconds=1)
    usage_date = date(2026, 7, 22)
    async with session_factory() as session:
        session.add(
            AgentUserDailyQuota(
                user_id=_USER_ID,
                usage_date=usage_date,
                used_count=7,
            )
        )
        queued_runs = [
            await _seed_run(
                session,
                status="queued",
                created_at=deadline_at - timedelta(seconds=60),
                deadline_at=deadline_at,
                quota_usage_date=usage_date,
            )
            for _ in range(6)
        ]
        running = await _seed_run(
            session,
            status="running",
            created_at=deadline_at - timedelta(seconds=60),
            deadline_at=deadline_at,
            attempt_epoch=4,
            quota_usage_date=usage_date,
        )
        await session.commit()

    async with session_factory() as session:
        async with session.begin():
            result = await AgentRunRepository(session).sweep_deadline_exceeded_runs(
                now=now
            )

    assert result.queued_quota_released_count == len(queued_runs)
    assert result.running_quota_reservation_count == 1
    for original in (*queued_runs, running):
        persisted = await _persisted_run(session_factory, original.id)
        assert persisted.status == "deadline_exceeded"
        assert persisted.error_code is None
        assert persisted.attempt_epoch == original.attempt_epoch

    async with session_factory() as session:
        async with session.begin():
            repeated = await AgentRunRepository(session).sweep_deadline_exceeded_runs(
                now=now + timedelta(seconds=1)
            )

    assert repeated.total_count == 0
    async with session_factory() as session:
        counter = await session.scalar(
            select(AgentUserDailyQuota.used_count).where(
                AgentUserDailyQuota.user_id == _USER_ID,
                AgentUserDailyQuota.usage_date == usage_date,
            )
        )
    assert counter == 1
    for original in (*queued_runs, running):
        persisted = await _persisted_run(session_factory, original.id)
        assert persisted.status == "deadline_exceeded"
        assert persisted.attempt_epoch == original.attempt_epoch


@pytest.mark.asyncio
async def test_sweep_terminalizes_when_queued_quota_is_ineligible_or_inconsistent(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """予約情報やカウンターが不整合でも時間切れを確定し、quotaを過剰に減算しない。"""
    now = datetime(2026, 7, 22, 12, 0, tzinfo=UTC)
    deadline_at = now - timedelta(microseconds=1)
    underflow_date = date(2026, 7, 21)
    missing_counter_date = date(2026, 7, 20)
    async with session_factory() as session:
        session.add(
            AgentUserDailyQuota(
                user_id=_USER_ID,
                usage_date=underflow_date,
                used_count=1,
            )
        )
        underflow_runs = [
            await _seed_run(
                session,
                status="queued",
                created_at=deadline_at - timedelta(seconds=60),
                deadline_at=deadline_at,
                quota_usage_date=underflow_date,
            )
            for _ in range(2)
        ]
        missing_counter = await _seed_run(
            session,
            status="queued",
            created_at=deadline_at - timedelta(seconds=60),
            deadline_at=deadline_at,
            quota_usage_date=missing_counter_date,
        )
        legacy = await _seed_run(
            session,
            status="queued",
            created_at=deadline_at - timedelta(seconds=60),
            deadline_at=deadline_at,
        )

        result = await AgentRunRepository(session).sweep_deadline_exceeded_runs(now=now)
        await session.commit()

    assert result.queued_quota_released_count == 0
    assert result.queued_quota_not_eligible_count == 1
    assert result.queued_quota_inconsistent_count == 3
    async with session_factory() as session:
        counter = await session.scalar(
            select(AgentUserDailyQuota.used_count).where(
                AgentUserDailyQuota.user_id == _USER_ID,
                AgentUserDailyQuota.usage_date == underflow_date,
            )
        )
    assert counter == 1
    for original in (*underflow_runs, missing_counter, legacy):
        persisted = await _persisted_run(session_factory, original.id)
        assert persisted.status == "deadline_exceeded"
        assert persisted.error_code is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status", ["completed", "failed", "policy_blocked", "deadline_exceeded"]
)
async def test_sweep_preserves_existing_terminal_runs(
    session_factory: async_sessionmaker[AsyncSession],
    status: str,
) -> None:
    """期限を超えても、既に確定した終端状態・回答参照・epochは上書きしない。"""
    now = datetime(2026, 7, 22, 12, 0, tzinfo=UTC)
    deadline_at = now - timedelta(microseconds=1)
    created_at = deadline_at - timedelta(seconds=60)
    async with session_factory() as session:
        original = await _seed_run(
            session,
            status=status,
            created_at=created_at,
            deadline_at=deadline_at,
            attempt_epoch=3,
        )
        assistant_message_id = original.assistant_message_id
        await session.commit()

        result = await AgentRunRepository(session).sweep_deadline_exceeded_runs(now=now)
        await session.commit()

    persisted = await _persisted_run(session_factory, original.id)
    assert result.total_count == 0
    assert persisted.status == status
    assert persisted.assistant_message_id == assistant_message_id
    assert persisted.error_code == ("internal_error" if status == "failed" else None)
    assert persisted.attempt_epoch == 3
