"""queued/running stale sweep の永続状態契約。"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from uuid import UUID

import pytest
from sqlalchemy import event as sa_event
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
    started_at: datetime | None = None,
    attempt_epoch: int = 0,
    quota_usage_date: date | None = None,
) -> AgentRun:
    thread = AgentThread(user_id=_USER_ID, title="stale sweep")
    session.add(thread)
    await session.flush()
    message = AgentMessage(
        thread_id=thread.id,
        seq=1,
        role="user",
        content="stale sweep question",
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
        started_at=started_at,
        attempt_epoch=attempt_epoch,
        error_code="internal_error" if status == "failed" else None,
        completed_at=created_at
        if status in {"completed", "failed", "policy_blocked"}
        else None,
        quota_usage_date=quota_usage_date,
    )
    session.add(run)
    await session.flush()
    return run


async def _status(
    session_factory: async_sessionmaker[AsyncSession], run_id: UUID
) -> tuple[str, str | None]:
    async with session_factory() as session:
        run = await session.get(AgentRun, run_id)
    assert run is not None
    return run.status, run.error_code


@pytest.mark.asyncio
async def test_sweep_uses_separate_exclusive_queued_and_running_cutoffs(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    now = datetime(2026, 7, 22, 12, 0, tzinfo=UTC)
    async with session_factory() as session:
        queued_expired = await _seed_run(
            session,
            status="queued",
            created_at=now - timedelta(seconds=300, microseconds=1),
        )
        queued_boundary = await _seed_run(
            session,
            status="queued",
            created_at=now - timedelta(seconds=300),
        )
        queued_fresh = await _seed_run(
            session,
            status="queued",
            created_at=now - timedelta(seconds=299, microseconds=999999),
        )
        running_expired = await _seed_run(
            session,
            status="running",
            created_at=now - timedelta(hours=1),
            started_at=now - timedelta(seconds=180, microseconds=1),
            attempt_epoch=2,
        )
        running_boundary = await _seed_run(
            session,
            status="running",
            created_at=now - timedelta(hours=1),
            started_at=now - timedelta(seconds=180),
            attempt_epoch=3,
        )
        running_fresh = await _seed_run(
            session,
            status="running",
            created_at=now - timedelta(hours=1),
            started_at=now - timedelta(seconds=179, microseconds=999999),
            attempt_epoch=4,
        )
        fallback_expired = await _seed_run(
            session,
            status="running",
            created_at=now - timedelta(seconds=180, microseconds=1),
            attempt_epoch=5,
        )
        fallback_boundary = await _seed_run(
            session,
            status="running",
            created_at=now - timedelta(seconds=180),
            attempt_epoch=6,
        )
        completed = await _seed_run(
            session,
            status="completed",
            created_at=now - timedelta(hours=1),
        )
        policy_blocked = await _seed_run(
            session,
            status="policy_blocked",
            created_at=now - timedelta(hours=1),
        )
        failed = await _seed_run(
            session,
            status="failed",
            created_at=now - timedelta(hours=1),
        )

        result = await AgentRunRepository(session).sweep_stale_runs(now=now)
        await session.commit()

    assert result.queued_terminal_count == 1
    assert result.queued_quota_not_eligible_count == 1
    assert result.queued_quota_released_count == 0
    assert result.queued_quota_inconsistent_count == 0
    assert {
        (running.run_id, running.attempt_epoch)
        for running in result.running_terminal_runs
    } == {
        (running_expired.id, 2),
        (fallback_expired.id, 5),
    }
    assert result.running_without_started_at_count == 1
    assert result.running_quota_reservation_count == 0
    assert await _status(session_factory, queued_expired.id) == ("failed", "stale")
    assert await _status(session_factory, running_expired.id) == ("failed", "stale")
    assert await _status(session_factory, fallback_expired.id) == ("failed", "stale")
    for untouched in (
        queued_boundary,
        queued_fresh,
        running_boundary,
        running_fresh,
        fallback_boundary,
        completed,
        policy_blocked,
        failed,
    ):
        assert (await _status(session_factory, untouched.id))[0] == untouched.status


@pytest.mark.asyncio
async def test_sweep_releases_queued_quota_once_per_group_without_running_refund(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    now = datetime(2026, 7, 22, 12, 0, tzinfo=UTC)
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
                created_at=now - timedelta(seconds=300, microseconds=1),
                quota_usage_date=usage_date,
            )
            for _ in range(6)
        ]
        running = await _seed_run(
            session,
            status="running",
            created_at=now - timedelta(hours=1),
            started_at=now - timedelta(seconds=180, microseconds=1),
            attempt_epoch=4,
            quota_usage_date=usage_date,
        )
        await session.commit()

    quota_updates: list[str] = []
    async with session_factory() as session:
        engine = session.sync_session.bind
        assert engine is not None

        def capture_quota_updates(
            _conn: object,
            _cursor: object,
            statement: str,
            _parameters: object,
            _context: object,
            _executemany: object,
        ) -> None:
            normalized = " ".join(statement.lower().split())
            if normalized.startswith("update agent_user_daily_quotas"):
                quota_updates.append(normalized)

        sa_event.listen(engine, "before_cursor_execute", capture_quota_updates)
        try:
            async with session.begin():
                result = await AgentRunRepository(session).sweep_stale_runs(now=now)
        finally:
            sa_event.remove(engine, "before_cursor_execute", capture_quota_updates)

    assert result.queued_terminal_count == len(queued_runs)
    assert result.queued_quota_released_count == len(queued_runs)
    assert result.running_quota_reservation_count == 1
    assert [
        (item.run_id, item.attempt_epoch) for item in result.running_terminal_runs
    ] == [(running.id, 4)]
    assert len(quota_updates) == 1
    async with session_factory() as session:
        counter = await session.scalar(
            select(AgentUserDailyQuota.used_count).where(
                AgentUserDailyQuota.user_id == _USER_ID,
                AgentUserDailyQuota.usage_date == usage_date,
            )
        )
    assert counter == 1
    assert [await _status(session_factory, run.id) for run in queued_runs] == [
        ("failed", "stale")
    ] * len(queued_runs)
    assert await _status(session_factory, running.id) == ("failed", "stale")


@pytest.mark.asyncio
async def test_sweep_terminalizes_when_queued_quota_is_ineligible_or_inconsistent(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    now = datetime(2026, 7, 22, 12, 0, tzinfo=UTC)
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
                created_at=now - timedelta(seconds=300, microseconds=1),
                quota_usage_date=underflow_date,
            )
            for _ in range(2)
        ]
        missing_counter = await _seed_run(
            session,
            status="queued",
            created_at=now - timedelta(seconds=300, microseconds=1),
            quota_usage_date=missing_counter_date,
        )
        legacy = await _seed_run(
            session,
            status="queued",
            created_at=now - timedelta(seconds=300, microseconds=1),
        )

        result = await AgentRunRepository(session).sweep_stale_runs(now=now)
        await session.commit()

    assert result.queued_terminal_count == 4
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
    for run in (*underflow_runs, missing_counter, legacy):
        assert await _status(session_factory, run.id) == ("failed", "stale")
