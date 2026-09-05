"""回答生成 repository の開始・再生成許可・継続確認のDB契約。"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent.answering.answer_generation_repository import (
    AgentAnswerGenerationRepository,
    _check_answer_generation_continuation,
    _start_answer_generation,
)
from app.agent.runs.execution import Continue, Stop, StopReason
from app.models.agent_run import AgentRun
from tests.agent.runs._seed import create_thread_message_run

pytestmark = pytest.mark.integration

_ATTEMPT_EPOCH = 3
_DEADLINE_AT = datetime(2026, 9, 5, 12, 1, tzinfo=UTC)
_BEFORE_DEADLINE = _DEADLINE_AT - timedelta(microseconds=1)
_ANSWER_STARTED_AT = _DEADLINE_AT - timedelta(seconds=5)


def _repository(
    session_factory: async_sessionmaker[AsyncSession],
    run_id: uuid.UUID,
    *,
    attempt_epoch: int = _ATTEMPT_EPOCH,
) -> AgentAnswerGenerationRepository:
    return AgentAnswerGenerationRepository(session_factory, run_id, attempt_epoch)


async def _seed_running(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    status: str = "running",
    error_code: str | None = None,
    answer_started_at: datetime | None = None,
    deadline_at: datetime = _DEADLINE_AT,
) -> uuid.UUID:
    async with session_factory() as session:
        _thread, _message, run = await create_thread_message_run(
            session,
            status=status,
            created_at=deadline_at - timedelta(seconds=60),
            deadline_at=deadline_at,
            attempt_epoch=_ATTEMPT_EPOCH,
            error_code=error_code,
        )
        if answer_started_at is not None:
            run.answer_started_at = answer_started_at
            await session.commit()
    return run.id


async def _load_run(
    session_factory: async_sessionmaker[AsyncSession],
    run_id: uuid.UUID,
) -> AgentRun:
    async with session_factory() as session:
        run = await session.get(AgentRun, run_id)
        assert run is not None
        return run


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


@pytest.mark.asyncio
async def test_start_current_attempt_before_deadline_records_database_time(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    run_id = await _seed_running(session_factory)

    result = await _repository(session_factory, run_id).start_answer_generation(
        now=_BEFORE_DEADLINE
    )

    run = await _load_run(session_factory, run_id)
    assert result == Continue()
    assert run.status == "running"
    assert run.answer_started_at == _BEFORE_DEADLINE


@pytest.mark.asyncio
async def test_start_at_deadline_expires_without_recording_start(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    run_id = await _seed_running(session_factory)

    result = await _repository(session_factory, run_id).start_answer_generation(
        now=_DEADLINE_AT
    )

    run = await _load_run(session_factory, run_id)
    assert result == Stop(StopReason.DEADLINE_EXCEEDED)
    assert run.status == "deadline_exceeded"
    assert run.answer_started_at is None


@pytest.mark.asyncio
async def test_start_stale_attempt_does_not_record_or_expire_run(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    run_id = await _seed_running(session_factory)

    result = await _repository(
        session_factory, run_id, attempt_epoch=_ATTEMPT_EPOCH - 1
    ).start_answer_generation(now=_DEADLINE_AT)

    run = await _load_run(session_factory, run_id)
    assert result == Stop(StopReason.NOT_CURRENT)
    assert run.status == "running"
    assert run.answer_started_at is None


@pytest.mark.asyncio
async def test_start_terminal_run_does_not_record_or_overwrite_status(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    run_id = await _seed_running(
        session_factory,
        status="failed",
        error_code="internal_error",
    )

    result = await _repository(session_factory, run_id).start_answer_generation(
        now=_BEFORE_DEADLINE
    )

    run = await _load_run(session_factory, run_id)
    assert result == Stop(StopReason.NOT_CURRENT)
    assert run.status == "failed"
    assert run.answer_started_at is None


@pytest.mark.asyncio
async def test_start_recorded_start_is_not_overwritten_on_redelivery(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    run_id = await _seed_running(
        session_factory,
        answer_started_at=_ANSWER_STARTED_AT,
    )

    result = await _repository(session_factory, run_id).start_answer_generation(
        now=_BEFORE_DEADLINE
    )

    run = await _load_run(session_factory, run_id)
    assert result == Stop(StopReason.NOT_CURRENT)
    assert run.status == "running"
    assert run.answer_started_at == _ANSWER_STARTED_AT


@pytest.mark.asyncio
async def test_start_missing_run_stops_without_write(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    result = await _repository(session_factory, uuid.uuid4()).start_answer_generation(
        now=_BEFORE_DEADLINE
    )

    assert result == Stop(StopReason.NOT_CURRENT)


@pytest.mark.asyncio
async def test_rolled_back_start_does_not_persist_generation_right(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    run_id = await _seed_running(session_factory)

    async with session_factory() as session:
        await session.begin()
        result = await _start_answer_generation(
            session,
            run_id=run_id,
            expected_attempt_epoch=_ATTEMPT_EPOCH,
            now=_BEFORE_DEADLINE,
        )
        await session.rollback()

    run = await _load_run(session_factory, run_id)
    assert result == Continue()
    assert run.answer_started_at is None


@pytest.mark.asyncio
async def test_start_commits_before_returning_success(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        database_time = await session.scalar(text("SELECT clock_timestamp()"))
    assert isinstance(database_time, datetime)
    run_id = await _seed_running(
        session_factory,
        deadline_at=database_time + timedelta(minutes=1),
    )

    result = await _repository(session_factory, run_id).start_answer_generation()

    run = await _load_run(session_factory, run_id)
    assert result == Continue()
    assert run.answer_started_at is not None


@pytest.mark.asyncio
async def test_lock_wait_uses_database_time_after_acquiring_run_lock(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    run_id = await _seed_running(session_factory)

    async with (
        session_factory() as locker,
        session_factory() as contender,
        session_factory() as observer,
    ):
        start_task: asyncio.Task[Continue | Stop] | None = None
        try:
            await locker.begin()
            locked_run = (
                await locker.execute(
                    select(AgentRun).where(AgentRun.id == run_id).with_for_update()
                )
            ).scalar_one()

            await contender.begin()
            contender_pid = await contender.scalar(text("SELECT pg_backend_pid()"))
            assert isinstance(contender_pid, int)
            start_task = asyncio.create_task(
                _start_answer_generation(
                    contender,
                    run_id=run_id,
                    expected_attempt_epoch=_ATTEMPT_EPOCH,
                )
            )
            await _wait_until_blocked(observer, contender_pid)

            locked_run.deadline_at = await locker.scalar(
                text("SELECT clock_timestamp()")
            )
            await locker.commit()

            result = await asyncio.wait_for(start_task, timeout=5)
            await contender.commit()
        finally:
            if start_task is not None:
                if not start_task.done():
                    start_task.cancel()
                await asyncio.gather(start_task, return_exceptions=True)
            for session in (locker, contender, observer):
                if session.in_transaction():
                    await session.rollback()

    run = await _load_run(session_factory, run_id)
    assert result == Stop(StopReason.DEADLINE_EXCEEDED)
    assert run.status == "deadline_exceeded"
    assert run.answer_started_at is None


@pytest.mark.asyncio
async def test_authorize_before_deadline_keeps_start_time(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    run_id = await _seed_running(
        session_factory,
        answer_started_at=_ANSWER_STARTED_AT,
    )

    result = await _repository(session_factory, run_id).authorize_answer_regeneration(
        now=_BEFORE_DEADLINE
    )

    run = await _load_run(session_factory, run_id)
    assert result == Continue()
    assert run.status == "running"
    assert run.answer_started_at == _ANSWER_STARTED_AT


@pytest.mark.asyncio
async def test_authorize_at_deadline_expires_without_changing_start_time(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    run_id = await _seed_running(
        session_factory,
        answer_started_at=_ANSWER_STARTED_AT,
    )

    result = await _repository(session_factory, run_id).authorize_answer_regeneration(
        now=_DEADLINE_AT
    )

    run = await _load_run(session_factory, run_id)
    assert result == Stop(StopReason.DEADLINE_EXCEEDED)
    assert run.status == "deadline_exceeded"
    assert run.answer_started_at == _ANSWER_STARTED_AT


@pytest.mark.asyncio
async def test_authorize_without_start_does_not_expire(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    run_id = await _seed_running(session_factory)

    result = await _repository(session_factory, run_id).authorize_answer_regeneration(
        now=_DEADLINE_AT
    )

    run = await _load_run(session_factory, run_id)
    assert result == Stop(StopReason.NOT_CURRENT)
    assert run.status == "running"
    assert run.answer_started_at is None


@pytest.mark.asyncio
async def test_authorize_stale_attempt_does_not_change_run(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    run_id = await _seed_running(
        session_factory,
        answer_started_at=_ANSWER_STARTED_AT,
    )

    result = await _repository(
        session_factory, run_id, attempt_epoch=_ATTEMPT_EPOCH - 1
    ).authorize_answer_regeneration(now=_DEADLINE_AT)

    run = await _load_run(session_factory, run_id)
    assert result == Stop(StopReason.NOT_CURRENT)
    assert run.status == "running"
    assert run.answer_started_at == _ANSWER_STARTED_AT


@pytest.mark.asyncio
async def test_check_continues_after_original_deadline(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        database_time = await session.scalar(text("SELECT clock_timestamp()"))
    assert isinstance(database_time, datetime)
    run_id = await _seed_running(
        session_factory,
        deadline_at=database_time - timedelta(seconds=1),
        answer_started_at=database_time - timedelta(seconds=5),
    )

    result = await _repository(
        session_factory, run_id
    ).check_answer_generation_continuation()

    run = await _load_run(session_factory, run_id)
    assert result == Continue()
    assert run.status == "running"
    assert run.answer_started_at == database_time - timedelta(seconds=5)


@pytest.mark.asyncio
async def test_check_stop_does_not_write_for_non_running_or_unstarted(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    missing = await _repository(
        session_factory, uuid.uuid4()
    ).check_answer_generation_continuation()
    assert missing == Stop(StopReason.NOT_CURRENT)

    unstarted_id = await _seed_running(session_factory)
    unstarted = await _repository(
        session_factory, unstarted_id
    ).check_answer_generation_continuation()
    unstarted_run = await _load_run(session_factory, unstarted_id)
    assert unstarted == Stop(StopReason.NOT_CURRENT)
    assert unstarted_run.status == "running"
    assert unstarted_run.answer_started_at is None

    stale_id = await _seed_running(
        session_factory,
        answer_started_at=_ANSWER_STARTED_AT,
    )
    stale = await _repository(
        session_factory, stale_id, attempt_epoch=_ATTEMPT_EPOCH - 1
    ).check_answer_generation_continuation()
    stale_run = await _load_run(session_factory, stale_id)
    assert stale == Stop(StopReason.NOT_CURRENT)
    assert stale_run.status == "running"
    assert stale_run.answer_started_at == _ANSWER_STARTED_AT

    failed_id = await _seed_running(
        session_factory,
        status="failed",
        error_code="internal_error",
        answer_started_at=_ANSWER_STARTED_AT,
    )
    failed = await _repository(
        session_factory, failed_id
    ).check_answer_generation_continuation()
    failed_run = await _load_run(session_factory, failed_id)
    assert failed == Stop(StopReason.NOT_CURRENT)
    assert failed_run.status == "failed"


@pytest.mark.asyncio
async def test_check_is_read_only_without_row_lock(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    run_id = await _seed_running(
        session_factory,
        answer_started_at=_ANSWER_STARTED_AT,
    )

    async with session_factory() as session:
        async with session.begin():
            result = await _check_answer_generation_continuation(
                session,
                run_id=run_id,
                expected_attempt_epoch=_ATTEMPT_EPOCH,
            )

    run = await _load_run(session_factory, run_id)
    assert result == Continue()
    assert run.status == "running"
    assert run.answer_started_at == _ANSWER_STARTED_AT
