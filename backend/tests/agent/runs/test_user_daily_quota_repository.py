"""AgentRunRepository の日次quota admission契約。"""

from __future__ import annotations

import asyncio
import inspect
import uuid
from collections.abc import Callable
from datetime import UTC, date, datetime

import pytest
from sqlalchemy import DateTime, func, literal, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import app.agent.runs.contracts as run_contracts
import app.agent.runs.repository as repository_module
from app.agent.runs.contracts import ActiveRunConflictError, ThreadNotFoundError
from app.agent.runs.repository import AgentRunRepository
from app.models.agent_message import AgentMessage
from app.models.agent_run import AgentRun
from app.models.agent_thread import AgentThread
from app.models.agent_user_daily_quota import AgentUserDailyQuota
from tests.conftest import TEST_ADMIN_ID, TEST_USER_ID

_DAILY_LIMIT = 10
_JST_DAY_BEFORE_MIDNIGHT = date(2026, 7, 20)
_JST_DAY_AFTER_MIDNIGHT = date(2026, 7, 21)
_BEFORE_MIDNIGHT_UTC = datetime(2026, 7, 20, 14, 59, 59, tzinfo=UTC)
_AFTER_MIDNIGHT_UTC = datetime(2026, 7, 20, 15, 0, 0, tzinfo=UTC)

pytestmark = pytest.mark.integration


def _reservation_statement_builder() -> Callable[..., object]:
    builder = getattr(
        repository_module,
        "_build_daily_quota_reservation_statement",
        None,
    )
    assert callable(builder), (
        "private daily quota reservation statement builder is not implemented"
    )
    return builder


def _fixed_timestamptz_expression(value: datetime) -> object:
    return literal(value, type_=DateTime(timezone=True))


def _patch_reservation_clock(
    monkeypatch: pytest.MonkeyPatch,
    *,
    observed_at: datetime,
) -> None:
    builder = _reservation_statement_builder()
    original_builder = getattr(
        builder,
        "_original_daily_quota_reservation_statement_builder",
        builder,
    )
    assert callable(original_builder)
    fixed_clock = _fixed_timestamptz_expression(observed_at)

    def build_with_fixed_clock(
        *,
        user_id: uuid.UUID,
        clock_expression: object | None = None,
    ) -> object:
        assert clock_expression is None
        return original_builder(user_id=user_id, clock_expression=fixed_clock)

    setattr(
        build_with_fixed_clock,
        "_original_daily_quota_reservation_statement_builder",
        original_builder,
    )

    monkeypatch.setattr(
        repository_module,
        "_build_daily_quota_reservation_statement",
        build_with_fixed_clock,
    )


async def _admit_new_thread(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    user_id: uuid.UUID,
    question: str,
    now: datetime | None = None,
) -> object:
    async with session_factory() as session:
        async with session.begin():
            return await AgentRunRepository(session).create_user_run(
                user_id=user_id,
                question=question,
                thread_id=None,
                now=now,
            )


async def _admission_state(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    usage_date: date,
) -> tuple[int, int, int, int | None]:
    thread_count = await session.scalar(
        select(func.count())
        .select_from(AgentThread)
        .where(AgentThread.user_id == user_id)
    )
    message_count = await session.scalar(
        select(func.count())
        .select_from(AgentMessage)
        .join(AgentThread, AgentMessage.thread_id == AgentThread.id)
        .where(AgentThread.user_id == user_id)
    )
    run_count = await session.scalar(
        select(func.count())
        .select_from(AgentRun)
        .join(AgentThread, AgentRun.thread_id == AgentThread.id)
        .where(AgentThread.user_id == user_id)
    )
    used_count = await session.scalar(
        select(AgentUserDailyQuota.used_count).where(
            AgentUserDailyQuota.user_id == user_id,
            AgentUserDailyQuota.usage_date == usage_date,
        )
    )
    return (
        int(thread_count or 0),
        int(message_count or 0),
        int(run_count or 0),
        used_count,
    )


async def _seed_active_thread(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    user_id: uuid.UUID,
) -> uuid.UUID:
    async with session_factory() as session:
        async with session.begin():
            thread = AgentThread(user_id=user_id, title="active")
            session.add(thread)
            await session.flush()
            message = AgentMessage(
                thread_id=thread.id,
                seq=1,
                role="user",
                content="already active",
                missing_aspects=[],
            )
            session.add(message)
            await session.flush()
            session.add(
                AgentRun(
                    thread_id=thread.id,
                    user_message_id=message.id,
                    status="queued",
                )
            )
        return thread.id


def _quota_rejection_fields(exc: Exception) -> tuple[date, datetime, datetime, int]:
    assert type(exc).__module__ == run_contracts.__name__
    usage_date = getattr(exc, "usage_date", None)
    observed_at = getattr(exc, "observed_at", None)
    decided_at = getattr(exc, "decided_at", None)
    limit = getattr(exc, "limit", None)
    assert isinstance(usage_date, date)
    assert isinstance(observed_at, datetime)
    assert isinstance(decided_at, datetime)
    assert isinstance(limit, int)
    return usage_date, observed_at, decided_at, limit


def test_create_user_run_does_not_accept_quota_limit_or_clock_inputs() -> None:
    parameters = inspect.signature(AgentRunRepository.create_user_run).parameters

    assert "limit" not in parameters
    assert "daily_limit" not in parameters
    assert "clock" not in parameters
    assert "clock_expression" not in parameters


def test_daily_quota_statement_builder_has_private_fixed_clock_seam() -> None:
    parameters = inspect.signature(_reservation_statement_builder()).parameters

    assert list(parameters) == ["user_id", "clock_expression"]
    assert parameters["clock_expression"].default is None


@pytest.mark.asyncio
async def test_create_user_run_reserves_quota_and_persists_same_usage_date(
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_reservation_clock(monkeypatch, observed_at=_BEFORE_MIDNIGHT_UTC)
    created = await _admit_new_thread(
        session_factory,
        user_id=uuid.UUID(TEST_USER_ID),
        question="JST usage date",
        now=datetime(2001, 1, 1, tzinfo=UTC),
    )
    run_id = getattr(created, "run_id", None)
    usage_date = getattr(created, "usage_date", None)
    used_count = getattr(created, "used_count", None)

    assert usage_date == _JST_DAY_BEFORE_MIDNIGHT
    assert used_count == 1
    assert isinstance(run_id, uuid.UUID)
    run = await db_session.get(AgentRun, run_id)
    assert run is not None
    assert run.quota_usage_date == _JST_DAY_BEFORE_MIDNIGHT
    assert await _admission_state(
        db_session,
        user_id=uuid.UUID(TEST_USER_ID),
        usage_date=_JST_DAY_BEFORE_MIDNIGHT,
    ) == (1, 1, 1, 1)


@pytest.mark.asyncio
async def test_daily_quota_statement_projects_each_side_of_jst_midnight(
    db_session: AsyncSession,
) -> None:
    builder = _reservation_statement_builder()
    user_id = uuid.UUID(TEST_USER_ID)
    before = (
        (
            await db_session.execute(
                builder(
                    user_id=user_id,
                    clock_expression=_fixed_timestamptz_expression(
                        _BEFORE_MIDNIGHT_UTC
                    ),
                )
            )
        )
        .mappings()
        .one()
    )
    after = (
        (
            await db_session.execute(
                builder(
                    user_id=user_id,
                    clock_expression=_fixed_timestamptz_expression(_AFTER_MIDNIGHT_UTC),
                )
            )
        )
        .mappings()
        .one()
    )

    assert before["usage_date"] == _JST_DAY_BEFORE_MIDNIGHT
    assert before["used_count"] == 1
    assert after["usage_date"] == _JST_DAY_AFTER_MIDNIGHT
    assert after["used_count"] == 1


@pytest.mark.asyncio
async def test_quota_statement_preserves_observed_at_while_waiting_for_row_lock(
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
) -> None:
    user_id = uuid.UUID(TEST_USER_ID)
    usage_date = await db_session.scalar(
        text("SELECT (clock_timestamp() AT TIME ZONE 'Asia/Tokyo')::date")
    )
    assert isinstance(usage_date, date)
    db_session.add(
        AgentUserDailyQuota(
            user_id=user_id,
            usage_date=usage_date,
            used_count=0,
        )
    )
    await db_session.commit()

    async with (
        session_factory() as locker,
        session_factory() as contender,
        session_factory() as observer,
    ):
        statement_task = None
        try:
            await locker.begin()
            lock_result = await locker.execute(
                text(
                    """
                    UPDATE agent_user_daily_quotas
                    SET used_count = used_count
                    WHERE user_id = :user_id AND usage_date = :usage_date
                    """
                ),
                {"user_id": user_id, "usage_date": usage_date},
            )
            assert lock_result.rowcount == 1

            await contender.begin()
            contender_pid = await contender.scalar(text("SELECT pg_backend_pid()"))
            assert isinstance(contender_pid, int)
            statement_task = asyncio.create_task(
                contender.execute(
                    _reservation_statement_builder()(
                        user_id=user_id,
                        clock_expression=None,
                    )
                )
            )

            async with asyncio.timeout(5):
                while True:
                    await observer.execute(text("SELECT pg_stat_clear_snapshot()"))
                    is_waiting_for_lock = await observer.scalar(
                        text(
                            """
                            SELECT wait_event_type = 'Lock'
                                   AND cardinality(pg_blocking_pids(pid)) > 0
                            FROM pg_stat_activity
                            WHERE pid = :pid
                            """
                        ),
                        {"pid": contender_pid},
                    )
                    if is_waiting_for_lock:
                        break
                    await asyncio.sleep(0.01)

            release_marker = await observer.scalar(text("SELECT clock_timestamp()"))
            assert isinstance(release_marker, datetime)
            await locker.commit()
            reservation_result = await asyncio.wait_for(statement_task, timeout=5)
            reservation = reservation_result.mappings().one()
            await contender.commit()

            observed_at = reservation["observed_at"]
            decided_at = reservation["decided_at"]
            assert isinstance(observed_at, datetime)
            assert isinstance(decided_at, datetime)
            assert observed_at <= release_marker <= decided_at
            assert reservation["usage_date"] == usage_date
            assert reservation["used_count"] == 1
        finally:
            if statement_task is not None:
                if not statement_task.done():
                    statement_task.cancel()
                await asyncio.gather(statement_task, return_exceptions=True)
            for session in (locker, contender, observer):
                if session.in_transaction():
                    await session.rollback()


@pytest.mark.asyncio
async def test_tenth_admission_succeeds_and_eleventh_is_typed_rejection_without_writes(
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_reservation_clock(monkeypatch, observed_at=_BEFORE_MIDNIGHT_UTC)
    user_id = uuid.UUID(TEST_USER_ID)
    accepted = [
        await _admit_new_thread(
            session_factory,
            user_id=user_id,
            question=f"request-{index}",
        )
        for index in range(_DAILY_LIMIT)
    ]

    with pytest.raises(Exception) as exc_info:
        await _admit_new_thread(
            session_factory,
            user_id=user_id,
            question="rejected-eleventh",
        )

    usage_date, observed_at, decided_at, limit = _quota_rejection_fields(exc_info.value)
    assert len(accepted) == _DAILY_LIMIT
    assert usage_date == _JST_DAY_BEFORE_MIDNIGHT
    assert observed_at.tzinfo is not None
    assert decided_at.tzinfo is not None
    assert limit == _DAILY_LIMIT
    assert await _admission_state(
        db_session,
        user_id=user_id,
        usage_date=_JST_DAY_BEFORE_MIDNIGHT,
    ) == (_DAILY_LIMIT, _DAILY_LIMIT, _DAILY_LIMIT, _DAILY_LIMIT)


@pytest.mark.asyncio
async def test_missing_or_active_existing_thread_does_not_reserve_quota(
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_reservation_clock(monkeypatch, observed_at=_BEFORE_MIDNIGHT_UTC)
    user_id = uuid.UUID(TEST_USER_ID)
    foreign_thread_id = await _seed_active_thread(
        session_factory,
        user_id=uuid.UUID(TEST_ADMIN_ID),
    )
    active_thread_id = await _seed_active_thread(session_factory, user_id=user_id)

    async with session_factory() as session:
        with pytest.raises(ThreadNotFoundError):
            async with session.begin():
                await AgentRunRepository(session).create_user_run(
                    user_id=user_id,
                    question="not owned",
                    thread_id=foreign_thread_id,
                )
        with pytest.raises(ActiveRunConflictError):
            async with session.begin():
                await AgentRunRepository(session).create_user_run(
                    user_id=user_id,
                    question="conflicts",
                    thread_id=active_thread_id,
                )

    assert await _admission_state(
        db_session,
        user_id=user_id,
        usage_date=_JST_DAY_BEFORE_MIDNIGHT,
    ) == (1, 1, 1, None)


@pytest.mark.asyncio
async def test_caller_rollback_removes_quota_thread_message_and_run(
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_reservation_clock(monkeypatch, observed_at=_BEFORE_MIDNIGHT_UTC)
    user_id = uuid.UUID(TEST_USER_ID)

    class RollBackAdmission(Exception):
        pass

    with pytest.raises(RollBackAdmission):
        async with session_factory() as session:
            async with session.begin():
                created = await AgentRunRepository(session).create_user_run(
                    user_id=user_id,
                    question="rollback all writes",
                    thread_id=None,
                )
                assert getattr(created, "usage_date", None) == _JST_DAY_BEFORE_MIDNIGHT
                raise RollBackAdmission()

    assert await _admission_state(
        db_session,
        user_id=user_id,
        usage_date=_JST_DAY_BEFORE_MIDNIGHT,
    ) == (0, 0, 0, None)


@pytest.mark.asyncio
async def test_quota_database_error_rolls_back_preflushed_new_thread(
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_id = uuid.UUID(TEST_USER_ID)

    def failing_builder(**_: object) -> object:
        return select(literal(1) / literal(0))

    monkeypatch.setattr(
        repository_module,
        "_build_daily_quota_reservation_statement",
        failing_builder,
        raising=False,
    )

    with pytest.raises(DBAPIError):
        async with session_factory() as session:
            async with session.begin():
                await AgentRunRepository(session).create_user_run(
                    user_id=user_id,
                    question="quota database failure",
                    thread_id=None,
                )

    assert await _admission_state(
        db_session,
        user_id=user_id,
        usage_date=_JST_DAY_BEFORE_MIDNIGHT,
    ) == (0, 0, 0, None)


@pytest.mark.asyncio
async def test_run_persistence_failure_rolls_back_reservation_and_all_writes(
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_reservation_clock(monkeypatch, observed_at=_BEFORE_MIDNIGHT_UTC)
    user_id = uuid.UUID(TEST_USER_ID)
    await db_session.execute(
        text(
            """
            CREATE FUNCTION fail_quota_test_agent_run_insert() RETURNS trigger
            LANGUAGE plpgsql AS $$
            BEGIN
              RAISE EXCEPTION 'quota test run insert failure';
            END;
            $$
            """
        )
    )
    await db_session.execute(
        text(
            """
            CREATE TRIGGER fail_quota_test_agent_run_insert
            BEFORE INSERT ON agent_runs
            FOR EACH ROW EXECUTE FUNCTION fail_quota_test_agent_run_insert()
            """
        )
    )
    await db_session.commit()

    try:
        with pytest.raises(DBAPIError):
            async with session_factory() as session:
                async with session.begin():
                    await AgentRunRepository(session).create_user_run(
                        user_id=user_id,
                        question="run persistence failure",
                        thread_id=None,
                    )
    finally:
        await db_session.execute(
            text("DROP TRIGGER fail_quota_test_agent_run_insert ON agent_runs")
        )
        await db_session.execute(text("DROP FUNCTION fail_quota_test_agent_run_insert"))
        await db_session.commit()

    assert await _admission_state(
        db_session,
        user_id=user_id,
        usage_date=_JST_DAY_BEFORE_MIDNIGHT,
    ) == (0, 0, 0, None)


@pytest.mark.asyncio
async def test_different_users_and_jst_dates_reserve_independent_counters(
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_reservation_clock(monkeypatch, observed_at=_BEFORE_MIDNIGHT_UTC)
    first_user_id = uuid.UUID(TEST_USER_ID)
    second_user_id = uuid.UUID(TEST_ADMIN_ID)
    first = await _admit_new_thread(
        session_factory,
        user_id=first_user_id,
        question="first user first day",
    )
    second = await _admit_new_thread(
        session_factory,
        user_id=second_user_id,
        question="second user first day",
    )
    _patch_reservation_clock(monkeypatch, observed_at=_AFTER_MIDNIGHT_UTC)
    next_day = await _admit_new_thread(
        session_factory,
        user_id=first_user_id,
        question="first user next day",
    )

    assert getattr(first, "used_count", None) == 1
    assert getattr(second, "used_count", None) == 1
    assert getattr(next_day, "used_count", None) == 1
    assert await _admission_state(
        db_session,
        user_id=first_user_id,
        usage_date=_JST_DAY_BEFORE_MIDNIGHT,
    ) == (2, 2, 2, 1)
    assert await _admission_state(
        db_session,
        user_id=second_user_id,
        usage_date=_JST_DAY_BEFORE_MIDNIGHT,
    ) == (1, 1, 1, 1)
    assert await _admission_state(
        db_session,
        user_id=first_user_id,
        usage_date=_JST_DAY_AFTER_MIDNIGHT,
    ) == (2, 2, 2, 1)


@pytest.mark.asyncio
async def test_eleven_concurrent_admissions_accept_exactly_ten(
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_reservation_clock(monkeypatch, observed_at=_BEFORE_MIDNIGHT_UTC)
    user_id = uuid.UUID(TEST_USER_ID)
    db_session.add(
        AgentUserDailyQuota(
            user_id=user_id,
            usage_date=_JST_DAY_BEFORE_MIDNIGHT,
            used_count=0,
        )
    )
    await db_session.commit()
    barrier = asyncio.Barrier(_DAILY_LIMIT + 1)

    async def admit(index: int) -> object:
        async with session_factory() as session:
            async with session.begin():
                await barrier.wait()
                return await AgentRunRepository(session).create_user_run(
                    user_id=user_id,
                    question=f"concurrent-{index}",
                    thread_id=None,
                )

    outcomes = await asyncio.gather(
        *(admit(index) for index in range(_DAILY_LIMIT + 1)),
        return_exceptions=True,
    )
    accepted = [outcome for outcome in outcomes if not isinstance(outcome, Exception)]
    rejected = [outcome for outcome in outcomes if isinstance(outcome, Exception)]

    assert len(accepted) == _DAILY_LIMIT
    assert len(rejected) == 1
    assert _quota_rejection_fields(rejected[0])[3] == _DAILY_LIMIT
    assert await _admission_state(
        db_session,
        user_id=user_id,
        usage_date=_JST_DAY_BEFORE_MIDNIGHT,
    ) == (_DAILY_LIMIT, _DAILY_LIMIT, _DAILY_LIMIT, _DAILY_LIMIT)
