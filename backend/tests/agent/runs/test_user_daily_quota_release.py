"""queued cancel による日次quota返却契約。"""

from __future__ import annotations

import asyncio
import inspect
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from importlib import import_module
from types import ModuleType

import pytest
from sqlalchemy import DateTime, literal, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import app.agent.runs.contracts as run_contracts
from app.agent.contract import AnswerQuestionResult, AnswerRetrievalSummary
from app.agent.runs.contracts import CancelRunOutcome
from app.agent.runs.repository import AgentRunRepository
from app.agent.runs.types import AgentRunErrorCode
from app.models.agent_message import AgentMessage
from app.models.agent_run import AgentRun
from app.models.agent_thread import AgentThread
from app.models.agent_user_daily_quota import AgentUserDailyQuota
from tests.conftest import TEST_ADMIN_ID, TEST_USER_ID

pytestmark = pytest.mark.integration

_USER_ID = uuid.UUID(TEST_USER_ID)
_ADMIN_ID = uuid.UUID(TEST_ADMIN_ID)
_USAGE_DATE = date(2026, 7, 19)
_CURRENT_DATE = date(2026, 7, 20)
_USAGE_CLOCK = datetime(2026, 7, 19, 3, 0, tzinfo=UTC)
_NOW = datetime(2026, 7, 20, 12, 0, tzinfo=UTC)
_MISSING = object()


@dataclass(frozen=True, slots=True)
class _SeededRun:
    run_id: uuid.UUID
    thread_id: uuid.UUID
    user_id: uuid.UUID
    quota_usage_date: date | None


async def _seed_run(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    user_id: uuid.UUID = _USER_ID,
    status: str = "queued",
    quota_usage_date: date | None = _USAGE_DATE,
    counter_count: int | None = 1,
    counter_usage_date: date | None = None,
    attempt_epoch: int = 0,
    created_at: datetime | None = None,
) -> _SeededRun:
    counter_date = counter_usage_date or quota_usage_date
    if counter_count is not None:
        assert counter_date is not None

    async with session_factory() as session:
        if counter_count is not None:
            session.add(
                AgentUserDailyQuota(
                    user_id=user_id,
                    usage_date=counter_date,
                    used_count=counter_count,
                )
            )
        thread = AgentThread(user_id=user_id, title="quota release")
        session.add(thread)
        await session.flush()
        user_message = AgentMessage(
            thread_id=thread.id,
            seq=1,
            role="user",
            content="quota release question",
            missing_aspects=[],
        )
        session.add(user_message)
        await session.flush()

        assistant_message_id = None
        if status == "completed":
            assistant_message = AgentMessage(
                thread_id=thread.id,
                seq=2,
                role="assistant",
                content="completed answer",
                missing_aspects=[],
            )
            session.add(assistant_message)
            await session.flush()
            assistant_message_id = assistant_message.id

        run = AgentRun(
            thread_id=thread.id,
            user_message_id=user_message.id,
            assistant_message_id=assistant_message_id,
            status=status,
            error_code="internal_error" if status == "failed" else None,
            started_at=_NOW if status == "running" else None,
            completed_at=_NOW if status in {"completed", "failed"} else None,
            attempt_epoch=attempt_epoch,
            quota_usage_date=quota_usage_date,
        )
        if created_at is not None:
            run.created_at = created_at
        session.add(run)
        await session.commit()
        seeded = _SeededRun(
            run_id=run.id,
            thread_id=thread.id,
            user_id=user_id,
            quota_usage_date=quota_usage_date,
        )

    async with session_factory() as verification:
        persisted_run = (
            await verification.execute(
                select(AgentRun.id, AgentRun.quota_usage_date).where(
                    AgentRun.id == seeded.run_id
                )
            )
        ).one_or_none()
        assert persisted_run == (seeded.run_id, quota_usage_date)
        if counter_count is not None:
            assert (
                await _counter(
                    verification,
                    user_id=user_id,
                    usage_date=counter_date,
                )
                == counter_count
            )
    return seeded


async def _counter(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    usage_date: date,
) -> int | None:
    return await session.scalar(
        select(AgentUserDailyQuota.used_count).where(
            AgentUserDailyQuota.user_id == user_id,
            AgentUserDailyQuota.usage_date == usage_date,
        )
    )


async def _read_counter(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    user_id: uuid.UUID,
    usage_date: date,
) -> int | None:
    async with session_factory() as session:
        return await _counter(session, user_id=user_id, usage_date=usage_date)


async def _cancel(
    session_factory: async_sessionmaker[AsyncSession],
    seeded: _SeededRun,
    *,
    user_id: uuid.UUID | None = None,
) -> object:
    async with session_factory() as session:
        async with session.begin():
            return await AgentRunRepository(session).cancel_run_for_user(
                run_id=seeded.run_id,
                user_id=user_id or seeded.user_id,
                now=_NOW,
            )


async def _lock_run_row(session: AsyncSession, run_id: uuid.UUID) -> None:
    result = await session.execute(
        text("UPDATE agent_runs SET status = status WHERE id = :run_id"),
        {"run_id": run_id},
    )
    assert result.rowcount == 1


async def _wait_until_blocked(observer: AsyncSession, backend_pid: int) -> None:
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
                {"pid": backend_pid},
            )
            if is_waiting_for_lock:
                return
            await asyncio.sleep(0.01)


def _reservation_statement(user_id: uuid.UUID) -> object:
    persistence = _daily_quota_persistence_module()
    builder = getattr(
        persistence,
        "_build_daily_quota_reservation_statement",
        None,
    )
    assert callable(builder)
    return builder(
        user_id=user_id,
        clock_expression=literal(_USAGE_CLOCK, type_=DateTime(timezone=True)),
    )


def _completed_result() -> AnswerQuestionResult:
    return AnswerQuestionResult(
        status="answered",
        answer="completed quota run",
        sources=[],
        missing_aspects=[],
        retrieval=AnswerRetrievalSummary(planned_mode="none"),
    )


def _daily_quota_contracts_module() -> ModuleType:
    try:
        return import_module("app.agent.runs.daily_quota.contracts")
    except ModuleNotFoundError as exc:
        if exc.name in {
            "app.agent.runs.daily_quota",
            "app.agent.runs.daily_quota.contracts",
        }:
            pytest.fail("app.agent.runs.daily_quota.contracts is not implemented")
        raise


def _daily_quota_persistence_module() -> ModuleType:
    try:
        return import_module("app.agent.runs.daily_quota.persistence")
    except ModuleNotFoundError as exc:
        if exc.name in {
            "app.agent.runs.daily_quota",
            "app.agent.runs.daily_quota.persistence",
        }:
            pytest.fail("app.agent.runs.daily_quota.persistence is not implemented")
        raise


def _quota_release_outcome(name: str) -> object:
    enum = getattr(_daily_quota_contracts_module(), "DailyQuotaReleaseOutcome", None)
    assert enum is not None, "DailyQuotaReleaseOutcome is not implemented"
    return getattr(enum, name)


def _assert_cancelled_release(
    result: object,
    *,
    release_outcome: str,
    was_running: bool,
    running_attempt_epoch: int | None,
) -> None:
    assert getattr(result, "cancel_outcome", None) is CancelRunOutcome.CANCELLED
    assert getattr(result, "was_running", None) is was_running
    assert getattr(result, "running_attempt_epoch", _MISSING) == running_attempt_epoch
    assert getattr(result, "quota_release_outcome", _MISSING) is _quota_release_outcome(
        release_outcome
    )
    assert not hasattr(result, "quota_usage_date")
    assert not hasattr(result, "quota_used_count")


def _assert_already_terminal(result: object, outcome: CancelRunOutcome) -> None:
    assert getattr(result, "cancel_outcome", None) is outcome
    assert getattr(result, "was_running", _MISSING) is False
    assert getattr(result, "running_attempt_epoch", _MISSING) is None
    assert getattr(result, "quota_release_outcome", _MISSING) is None
    assert not hasattr(result, "quota_usage_date")
    assert not hasattr(result, "quota_used_count")


def test_cancel_command_outcome_validates_run_and_quota_boundaries() -> None:
    command_outcome = getattr(run_contracts, "CancelRunCommandOutcome", None)
    assert command_outcome is not None, "CancelRunCommandOutcome is not implemented"
    assert not hasattr(run_contracts, "CancelRunResult")
    parameters = inspect.signature(command_outcome).parameters
    assert set(parameters) == {
        "cancel_outcome",
        "was_running",
        "running_attempt_epoch",
        "quota_release_outcome",
    }
    assert (
        not {
            "quota_usage_date",
            "quota_used_count",
        }
        & parameters.keys()
    )
    release_enum = getattr(
        _daily_quota_contracts_module(),
        "DailyQuotaReleaseOutcome",
        None,
    )
    assert release_enum is not None
    assert {member.value for member in release_enum} == {
        "released",
        "not_eligible",
        "inconsistent",
    }

    with pytest.raises(ValueError):
        command_outcome(
            cancel_outcome=CancelRunOutcome.CANCELLED,
            was_running=True,
        )
    with pytest.raises(ValueError):
        command_outcome(
            cancel_outcome=CancelRunOutcome.CANCELLED,
            was_running=True,
            running_attempt_epoch=0,
        )
    with pytest.raises(ValueError):
        command_outcome(
            cancel_outcome=CancelRunOutcome.CANCELLED,
            running_attempt_epoch=1,
        )
    with pytest.raises(ValueError):
        command_outcome(
            cancel_outcome=CancelRunOutcome.ALREADY_FAILED,
            was_running=True,
            running_attempt_epoch=1,
        )
    with pytest.raises(ValueError):
        command_outcome(
            cancel_outcome=CancelRunOutcome.ALREADY_FAILED,
            quota_release_outcome=release_enum.RELEASED,
        )


@pytest.mark.asyncio
async def test_quota_queued_cancel_releases_to_zero_and_keeps_marker_and_row(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    seeded = await _seed_run(session_factory)

    result = await _cancel(session_factory, seeded)

    async with session_factory() as session:
        run = await session.get(AgentRun, seeded.run_id)
        assert run is not None
        assert (run.status, run.error_code, run.quota_usage_date) == (
            "failed",
            "cancelled",
            _USAGE_DATE,
        )
        assert (
            await _counter(
                session,
                user_id=_USER_ID,
                usage_date=_USAGE_DATE,
            )
            == 0
        )
    _assert_cancelled_release(
        result,
        release_outcome="RELEASED",
        was_running=False,
        running_attempt_epoch=None,
    )


@pytest.mark.asyncio
async def test_double_cancel_refunds_once_and_second_result_is_not_release_event(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    seeded = await _seed_run(session_factory, counter_count=2)

    first = await _cancel(session_factory, seeded)
    second = await _cancel(session_factory, seeded)

    assert (
        await _read_counter(
            session_factory,
            user_id=_USER_ID,
            usage_date=_USAGE_DATE,
        )
        == 1
    )
    _assert_cancelled_release(
        first,
        release_outcome="RELEASED",
        was_running=False,
        running_attempt_epoch=None,
    )
    _assert_already_terminal(second, CancelRunOutcome.ALREADY_FAILED)


@pytest.mark.asyncio
async def test_running_quota_cancel_is_not_eligible_and_returns_execution_epoch(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    seeded = await _seed_run(session_factory, status="running", attempt_epoch=1)

    result = await _cancel(session_factory, seeded)

    assert (
        await _read_counter(
            session_factory,
            user_id=_USER_ID,
            usage_date=_USAGE_DATE,
        )
        == 1
    )
    _assert_cancelled_release(
        result,
        release_outcome="NOT_ELIGIBLE",
        was_running=True,
        running_attempt_epoch=1,
    )


@pytest.mark.asyncio
async def test_legacy_queued_cancel_is_not_eligible_without_counter(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    seeded = await _seed_run(
        session_factory,
        quota_usage_date=None,
        counter_count=None,
    )

    result = await _cancel(session_factory, seeded)

    _assert_cancelled_release(
        result,
        release_outcome="NOT_ELIGIBLE",
        was_running=False,
        running_attempt_epoch=None,
    )


@pytest.mark.parametrize("counter_count", [None, 0])
@pytest.mark.asyncio
async def test_missing_or_zero_counter_cancels_without_underflow_as_inconsistent(
    session_factory: async_sessionmaker[AsyncSession],
    counter_count: int | None,
) -> None:
    seeded = await _seed_run(session_factory, counter_count=counter_count)

    result = await _cancel(session_factory, seeded)

    assert (
        await _read_counter(
            session_factory,
            user_id=_USER_ID,
            usage_date=_USAGE_DATE,
        )
        == counter_count
    )
    _assert_cancelled_release(
        result,
        release_outcome="INCONSISTENT",
        was_running=False,
        running_attempt_epoch=None,
    )


@pytest.mark.asyncio
async def test_other_user_cannot_cancel_and_owner_releases_only_original_date(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    seeded = await _seed_run(session_factory, counter_count=2)
    async with session_factory() as setup:
        setup.add(
            AgentUserDailyQuota(
                user_id=_USER_ID,
                usage_date=_CURRENT_DATE,
                used_count=4,
            )
        )
        await setup.commit()

    denied = await _cancel(session_factory, seeded, user_id=_ADMIN_ID)
    released = await _cancel(session_factory, seeded)

    assert denied is None
    assert (
        await _read_counter(
            session_factory,
            user_id=_USER_ID,
            usage_date=_USAGE_DATE,
        )
        == 1
    )
    assert (
        await _read_counter(
            session_factory,
            user_id=_USER_ID,
            usage_date=_CURRENT_DATE,
        )
        == 4
    )
    _assert_cancelled_release(
        released,
        release_outcome="RELEASED",
        was_running=False,
        running_attempt_epoch=None,
    )


@pytest.mark.asyncio
async def test_cancel_winner_refunds_before_waiting_acquire_loses(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    seeded = await _seed_run(session_factory)

    async with (
        session_factory() as cancel_session,
        session_factory() as acquire_session,
        session_factory() as observer,
    ):
        acquire_task = None
        try:
            await cancel_session.begin()
            cancel_result = await AgentRunRepository(
                cancel_session
            ).cancel_run_for_user(
                run_id=seeded.run_id,
                user_id=_USER_ID,
                now=_NOW,
            )

            await acquire_session.begin()
            acquire_pid = await acquire_session.scalar(text("SELECT pg_backend_pid()"))
            assert isinstance(acquire_pid, int)
            acquire_task = asyncio.create_task(
                AgentRunRepository(acquire_session).acquire_for_execution(seeded.run_id)
            )
            await _wait_until_blocked(observer, acquire_pid)

            await cancel_session.commit()
            prepared = await asyncio.wait_for(acquire_task, timeout=5)
            await acquire_session.commit()
        finally:
            if acquire_task is not None:
                if not acquire_task.done():
                    acquire_task.cancel()
                await asyncio.gather(acquire_task, return_exceptions=True)
            for session in (cancel_session, acquire_session, observer):
                if session.in_transaction():
                    await session.rollback()

    assert prepared is None
    assert (
        await _read_counter(
            session_factory,
            user_id=_USER_ID,
            usage_date=_USAGE_DATE,
        )
        == 0
    )
    _assert_cancelled_release(
        cancel_result,
        release_outcome="RELEASED",
        was_running=False,
        running_attempt_epoch=None,
    )


@pytest.mark.asyncio
async def test_cancel_waiting_on_run_lock_uses_winning_status_update_for_release(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    seeded = await _seed_run(session_factory)

    async with (
        session_factory() as locker,
        session_factory() as contender,
        session_factory() as observer,
    ):
        cancel_task = None
        try:
            await locker.begin()
            await _lock_run_row(locker, seeded.run_id)

            await contender.begin()
            contender_pid = await contender.scalar(text("SELECT pg_backend_pid()"))
            assert isinstance(contender_pid, int)
            cancel_task = asyncio.create_task(
                AgentRunRepository(contender).cancel_run_for_user(
                    run_id=seeded.run_id,
                    user_id=_USER_ID,
                    now=_NOW,
                )
            )

            await _wait_until_blocked(observer, contender_pid)

            prepared = await AgentRunRepository(locker).acquire_for_execution(
                seeded.run_id
            )
            assert prepared is not None
            assert prepared.attempt_epoch == 1
            await locker.commit()

            cancel_result = await asyncio.wait_for(cancel_task, timeout=5)
            await contender.commit()
        finally:
            if cancel_task is not None:
                if not cancel_task.done():
                    cancel_task.cancel()
                await asyncio.gather(cancel_task, return_exceptions=True)
            for session in (locker, contender, observer):
                if session.in_transaction():
                    await session.rollback()

    assert (
        await _read_counter(
            session_factory,
            user_id=_USER_ID,
            usage_date=_USAGE_DATE,
        )
        == 1
    )
    async with session_factory() as verification:
        run = await verification.get(AgentRun, seeded.run_id)
        assert run is not None
        assert (run.status, run.error_code, run.attempt_epoch) == (
            "failed",
            "cancelled",
            1,
        )
    _assert_cancelled_release(
        cancel_result,
        release_outcome="NOT_ELIGIBLE",
        was_running=True,
        running_attempt_epoch=1,
    )


@pytest.mark.parametrize("winner", ["cancel", "acquire"])
@pytest.mark.asyncio
async def test_waiting_stale_sweep_rechecks_cancel_or_acquire_winner(
    session_factory: async_sessionmaker[AsyncSession],
    winner: str,
) -> None:
    seeded = await _seed_run(
        session_factory,
        created_at=_NOW - timedelta(minutes=21),
    )

    async with (
        session_factory() as winner_session,
        session_factory() as sweep_session,
        session_factory() as observer,
    ):
        sweep_task = None
        try:
            await winner_session.begin()
            if winner == "cancel":
                cancel_result = await AgentRunRepository(
                    winner_session
                ).cancel_run_for_user(
                    run_id=seeded.run_id,
                    user_id=_USER_ID,
                    now=_NOW,
                )
                _assert_cancelled_release(
                    cancel_result,
                    release_outcome="RELEASED",
                    was_running=False,
                    running_attempt_epoch=None,
                )
            else:
                prepared = await AgentRunRepository(
                    winner_session
                ).acquire_for_execution(
                    seeded.run_id,
                    now=_NOW,
                )
                assert prepared is not None and prepared.attempt_epoch == 1

            await sweep_session.begin()
            sweep_pid = await sweep_session.scalar(text("SELECT pg_backend_pid()"))
            assert isinstance(sweep_pid, int)
            sweep_task = asyncio.create_task(
                AgentRunRepository(sweep_session).sweep_stale_runs(now=_NOW)
            )
            await _wait_until_blocked(observer, sweep_pid)

            await winner_session.commit()
            sweep_result = await asyncio.wait_for(sweep_task, timeout=5)
            await sweep_session.commit()
        finally:
            if sweep_task is not None:
                if not sweep_task.done():
                    sweep_task.cancel()
                await asyncio.gather(sweep_task, return_exceptions=True)
            for session in (winner_session, sweep_session, observer):
                if session.in_transaction():
                    await session.rollback()

    assert (
        sweep_result.total_count,
        sweep_result.quota_queued_count,
        sweep_result.quota_running_count,
    ) == (0, 0, 0)
    async with session_factory() as verification:
        run = await verification.get(AgentRun, seeded.run_id)
        assert run is not None
        if winner == "cancel":
            assert (run.status, run.error_code, run.attempt_epoch) == (
                "failed",
                "cancelled",
                0,
            )
            expected_counter = 0
        else:
            assert (run.status, run.error_code, run.attempt_epoch) == (
                "running",
                None,
                1,
            )
            expected_counter = 1
    assert (
        await _read_counter(
            session_factory,
            user_id=_USER_ID,
            usage_date=_USAGE_DATE,
        )
        == expected_counter
    )


@pytest.mark.asyncio
async def test_mark_failed_never_refunds(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    seeded = await _seed_run(
        session_factory,
        status="running",
        attempt_epoch=3,
    )

    async with session_factory() as transition_session:
        async with transition_session.begin():
            changed = await AgentRunRepository(transition_session).mark_failed(
                seeded.run_id,
                expected_attempt_epoch=3,
                error_code=AgentRunErrorCode.INTERNAL_ERROR,
                now=_NOW,
            )
            assert changed is True

    result = await _cancel(session_factory, seeded)

    assert (
        await _read_counter(
            session_factory,
            user_id=_USER_ID,
            usage_date=_USAGE_DATE,
        )
        == 1
    )
    _assert_already_terminal(result, CancelRunOutcome.ALREADY_FAILED)


@pytest.mark.parametrize(
    ("transition", "status", "attempt_epoch", "expected_outcome"),
    [
        ("enqueue_failed", "queued", 0, CancelRunOutcome.ALREADY_FAILED),
        ("stale", "queued", 0, CancelRunOutcome.ALREADY_FAILED),
        ("complete", "running", 1, CancelRunOutcome.ALREADY_COMPLETED),
    ],
)
@pytest.mark.asyncio
async def test_competing_terminal_transition_wins_without_refund(
    session_factory: async_sessionmaker[AsyncSession],
    transition: str,
    status: str,
    attempt_epoch: int,
    expected_outcome: CancelRunOutcome,
) -> None:
    seeded = await _seed_run(
        session_factory,
        status=status,
        attempt_epoch=attempt_epoch,
        created_at=_NOW - timedelta(minutes=21) if transition == "stale" else None,
    )

    async with (
        session_factory() as locker,
        session_factory() as cancel_session,
        session_factory() as observer,
    ):
        cancel_task = None
        try:
            await locker.begin()
            await _lock_run_row(locker, seeded.run_id)

            await cancel_session.begin()
            cancel_pid = await cancel_session.scalar(text("SELECT pg_backend_pid()"))
            assert isinstance(cancel_pid, int)
            cancel_task = asyncio.create_task(
                AgentRunRepository(cancel_session).cancel_run_for_user(
                    run_id=seeded.run_id,
                    user_id=_USER_ID,
                    now=_NOW,
                )
            )
            await _wait_until_blocked(observer, cancel_pid)

            repository = AgentRunRepository(locker)
            if transition == "enqueue_failed":
                assert await repository.mark_enqueue_failed(
                    seeded.run_id,
                    now=_NOW,
                )
            elif transition == "stale":
                stale_result = await repository.sweep_stale_runs(now=_NOW)
                assert (
                    stale_result.total_count,
                    stale_result.quota_queued_count,
                    stale_result.quota_running_count,
                ) == (1, 1, 0)
            else:
                assert await repository.complete_run(
                    run_id=seeded.run_id,
                    result=_completed_result(),
                    expected_attempt_epoch=1,
                    now=_NOW,
                )
            await locker.commit()

            cancel_result = await asyncio.wait_for(cancel_task, timeout=5)
            await cancel_session.commit()
        finally:
            if cancel_task is not None:
                if not cancel_task.done():
                    cancel_task.cancel()
                await asyncio.gather(cancel_task, return_exceptions=True)
            for session in (locker, cancel_session, observer):
                if session.in_transaction():
                    await session.rollback()

    assert (
        await _read_counter(
            session_factory,
            user_id=_USER_ID,
            usage_date=_USAGE_DATE,
        )
        == 1
    )
    _assert_already_terminal(cancel_result, expected_outcome)


@pytest.mark.parametrize(
    ("status", "outcome"),
    [
        ("completed", CancelRunOutcome.ALREADY_COMPLETED),
        ("failed", CancelRunOutcome.ALREADY_FAILED),
    ],
)
@pytest.mark.asyncio
async def test_terminal_cancel_is_not_a_release_event(
    session_factory: async_sessionmaker[AsyncSession],
    status: str,
    outcome: CancelRunOutcome,
) -> None:
    seeded = await _seed_run(session_factory, status=status)

    result = await _cancel(session_factory, seeded)

    assert (
        await _read_counter(
            session_factory,
            user_id=_USER_ID,
            usage_date=_USAGE_DATE,
        )
        == 1
    )
    _assert_already_terminal(result, outcome)


@pytest.mark.asyncio
async def test_thread_delete_does_not_refund_quota(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    seeded = await _seed_run(session_factory)

    async with session_factory() as session:
        async with session.begin():
            thread = await session.get(AgentThread, seeded.thread_id)
            assert thread is not None
            await session.delete(thread)

    assert (
        await _read_counter(
            session_factory,
            user_id=_USER_ID,
            usage_date=_USAGE_DATE,
        )
        == 1
    )


@pytest.mark.parametrize("first_winner", ["release", "reserve_rejection"])
@pytest.mark.asyncio
async def test_release_and_reserve_linearize_in_quota_row_lock_order(
    session_factory: async_sessionmaker[AsyncSession],
    first_winner: str,
) -> None:
    seeded = await _seed_run(session_factory, counter_count=10)

    async with (
        session_factory() as winner,
        session_factory() as contender,
        session_factory() as observer,
    ):
        contender_task = None
        try:
            await winner.begin()
            if first_winner == "release":
                release_result = await AgentRunRepository(winner).cancel_run_for_user(
                    run_id=seeded.run_id,
                    user_id=_USER_ID,
                    now=_NOW,
                )

                await contender.begin()
                contender_pid = await contender.scalar(text("SELECT pg_backend_pid()"))
                assert isinstance(contender_pid, int)
                contender_task = asyncio.create_task(
                    contender.execute(_reservation_statement(_USER_ID))
                )
                await _wait_until_blocked(observer, contender_pid)

                await winner.commit()
                reservation_result = await asyncio.wait_for(
                    contender_task,
                    timeout=5,
                )
                await contender.commit()
                reservation = reservation_result.mappings().one()
                assert reservation["used_count"] == 10
                expected_count = 10
            else:
                rejection = (
                    (await winner.execute(_reservation_statement(_USER_ID)))
                    .mappings()
                    .one()
                )
                assert rejection["used_count"] is None

                await contender.begin()
                contender_pid = await contender.scalar(text("SELECT pg_backend_pid()"))
                assert isinstance(contender_pid, int)
                contender_task = asyncio.create_task(
                    AgentRunRepository(contender).cancel_run_for_user(
                        run_id=seeded.run_id,
                        user_id=_USER_ID,
                        now=_NOW,
                    )
                )
                await _wait_until_blocked(observer, contender_pid)

                await winner.commit()
                release_result = await asyncio.wait_for(contender_task, timeout=5)
                await contender.commit()
                expected_count = 9
        finally:
            if contender_task is not None:
                if not contender_task.done():
                    contender_task.cancel()
                await asyncio.gather(contender_task, return_exceptions=True)
            for session in (winner, contender, observer):
                if session.in_transaction():
                    await session.rollback()

    assert (
        await _read_counter(
            session_factory,
            user_id=_USER_ID,
            usage_date=_USAGE_DATE,
        )
        == expected_count
    )
    _assert_cancelled_release(
        release_result,
        release_outcome="RELEASED",
        was_running=False,
        running_attempt_epoch=None,
    )
