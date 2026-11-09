"""回答開始・再生成許可を AgentRunStateRepository とその窓口から実DBで確かめる。

前提: 時刻を固定する判定は repository を直接呼び、窓口(AgentAnswerGenerationRepository)
は AgentRunStateService 経由でDB時刻のまま呼ぶ。
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent.live_updates.stream import AgentRunLiveStreamEvent
from app.agent.running.answer_generation import AgentAnswerGenerationRepository
from app.agent.running.finish_notice import ClosedRun, ClosedRunningRun
from app.agent.running.state import (
    AnswerGenerationStarted,
    AttemptSuperseded,
    CanContinue,
    CannotContinue,
    DeadlineExceeded,
    RunNotFound,
    RunPhaseMismatchError,
)
from app.agent.running.state_repository import AgentRunStateRepository
from app.agent.running.state_service import AgentRunStateService
from app.agent.running.stop import AnswerGenerationStopped
from app.agent.running.types import AgentRunStatus
from app.models.agent_run import AgentRun
from tests.agent.running._seed import create_thread_message_run

pytestmark = pytest.mark.integration

_ATTEMPT_EPOCH = 3
_DEADLINE_AT = datetime(2026, 9, 5, 12, 1, tzinfo=UTC)
_BEFORE_DEADLINE = _DEADLINE_AT - timedelta(microseconds=1)
_ANSWER_STARTED_AT = _DEADLINE_AT - timedelta(seconds=5)


class _RecordingPublisher:
    def __init__(self) -> None:
        self.events: list[tuple[uuid.UUID, int, AgentRunLiveStreamEvent]] = []

    def publisher_for(self, run_id: uuid.UUID, attempt_epoch: int) -> _EpochPublisher:
        return _EpochPublisher(self, run_id, attempt_epoch)


class _EpochPublisher:
    def __init__(
        self, recorder: _RecordingPublisher, run_id: uuid.UUID, attempt_epoch: int
    ) -> None:
        self._recorder = recorder
        self._run_id = run_id
        self._attempt_epoch = attempt_epoch

    async def publish(self, event: AgentRunLiveStreamEvent) -> str | None:
        self._recorder.events.append((self._run_id, self._attempt_epoch, event))
        return None


def _repository(
    session_factory: async_sessionmaker[AsyncSession],
    run_id: uuid.UUID,
    *,
    attempt_epoch: int = _ATTEMPT_EPOCH,
    publisher: _RecordingPublisher | None = None,
) -> AgentAnswerGenerationRepository:
    recorder = publisher or _RecordingPublisher()
    return AgentAnswerGenerationRepository(
        AgentRunStateService(
            session_factory=session_factory,
            publisher_for=recorder.publisher_for,  # type: ignore[arg-type]
        ),
        run_id,
        attempt_epoch,
    )


async def _start_at(
    session_factory: async_sessionmaker[AsyncSession],
    run_id: uuid.UUID,
    *,
    now: datetime,
    attempt_epoch: int = _ATTEMPT_EPOCH,
) -> tuple[AnswerGenerationStarted | CannotContinue, list[ClosedRun]]:
    async with session_factory() as session:
        async with session.begin():
            repository = AgentRunStateRepository(session)
            result = await repository.start_answer_generation(
                run_id, attempt_epoch=attempt_epoch, now=now
            )
    return result, repository.closed_runs


async def _authorize_at(
    session_factory: async_sessionmaker[AsyncSession],
    run_id: uuid.UUID,
    *,
    now: datetime,
    attempt_epoch: int = _ATTEMPT_EPOCH,
) -> tuple[CanContinue | CannotContinue, list[ClosedRun]]:
    async with session_factory() as session:
        async with session.begin():
            repository = AgentRunStateRepository(session)
            result = await repository.authorize_answer_regeneration(
                run_id, attempt_epoch=attempt_epoch, now=now
            )
    return result, repository.closed_runs


async def _database_time(session_factory: async_sessionmaker[AsyncSession]) -> datetime:
    async with session_factory() as session:
        database_time = await session.scalar(text("SELECT clock_timestamp()"))
    assert isinstance(database_time, datetime)
    return database_time


def _closed_by_deadline(run_id: uuid.UUID) -> list[ClosedRun]:
    return [
        ClosedRunningRun(
            run_id=run_id,
            attempt_epoch=_ATTEMPT_EPOCH,
            status=AgentRunStatus.DEADLINE_EXCEEDED,
            error_code=None,
        )
    ]


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
    """期限前の現在の世代は回答を開始し、その時刻を開始時刻として記録する。"""
    run_id = await _seed_running(session_factory)

    result = await _start_at(session_factory, run_id, now=_BEFORE_DEADLINE)

    run = await _load_run(session_factory, run_id)
    assert result == (AnswerGenerationStarted(run_id, _BEFORE_DEADLINE), [])
    assert run.status == "running"
    assert run.answer_started_at == _BEFORE_DEADLINE


@pytest.mark.asyncio
async def test_start_at_deadline_expires_without_recording_start(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """期限ちょうどの回答開始はRunを期限切れで終えてその世代へ知らせ、開始時刻を記録しない。"""
    run_id = await _seed_running(session_factory)

    result = await _start_at(session_factory, run_id, now=_DEADLINE_AT)

    run = await _load_run(session_factory, run_id)
    assert result == (DeadlineExceeded(), _closed_by_deadline(run_id))
    assert run.status == "deadline_exceeded"
    assert run.answer_started_at is None


@pytest.mark.asyncio
async def test_start_stale_attempt_does_not_record_or_expire_run(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """古い世代の回答開始は期限より先に別の試行が担当していると返し、Runを記録も期限切れにもしない。"""
    run_id = await _seed_running(session_factory)

    result = await _start_at(
        session_factory, run_id, now=_DEADLINE_AT, attempt_epoch=_ATTEMPT_EPOCH - 1
    )

    run = await _load_run(session_factory, run_id)
    assert result == (AttemptSuperseded(), [])
    assert run.status == "running"
    assert run.answer_started_at is None


@pytest.mark.asyncio
async def test_start_terminal_run_does_not_record_or_overwrite_status(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """失敗で終わったRunの回答開始は別の試行が担当したと返し、状態も開始時刻も書かない。"""
    run_id = await _seed_running(
        session_factory,
        status="failed",
        error_code="internal_error",
    )

    result = await _start_at(session_factory, run_id, now=_BEFORE_DEADLINE)

    run = await _load_run(session_factory, run_id)
    assert result == (AttemptSuperseded(), [])
    assert run.status == "failed"
    assert run.answer_started_at is None


@pytest.mark.asyncio
async def test_start_twice_in_same_attempt_does_not_overwrite_recorded_start(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """同じ試行が回答を2度開始すると呼び出し順の誤りとして例外になり、記録済みの開始時刻を変えない。"""
    run_id = await _seed_running(
        session_factory,
        answer_started_at=_ANSWER_STARTED_AT,
    )

    with pytest.raises(RunPhaseMismatchError):
        await _start_at(session_factory, run_id, now=_BEFORE_DEADLINE)

    run = await _load_run(session_factory, run_id)
    assert run.status == "running"
    assert run.answer_started_at == _ANSWER_STARTED_AT


@pytest.mark.asyncio
async def test_start_missing_run_stops_without_write(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """存在しないRunの回答開始は、窓口が見つからないことを止まった理由として送出する。"""
    with pytest.raises(AnswerGenerationStopped) as stopped:
        await _repository(session_factory, uuid.uuid4()).start_answer_generation()

    assert stopped.value.stopped == RunNotFound()


@pytest.mark.asyncio
async def test_rolled_back_start_does_not_persist_generation_right(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """巻き戻した回答開始は開始時刻を残さない。"""
    run_id = await _seed_running(session_factory)

    async with session_factory() as session:
        await session.begin()
        result = await AgentRunStateRepository(session).start_answer_generation(
            run_id,
            attempt_epoch=_ATTEMPT_EPOCH,
            now=_BEFORE_DEADLINE,
        )
        await session.rollback()

    run = await _load_run(session_factory, run_id)
    assert result == AnswerGenerationStarted(run_id, _BEFORE_DEADLINE)
    assert run.answer_started_at is None


@pytest.mark.asyncio
async def test_start_commits_before_returning_success(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """窓口の回答開始は、開始時刻をcommitしてから開始できたと返す。"""
    run_id = await _seed_running(
        session_factory,
        deadline_at=await _database_time(session_factory) + timedelta(minutes=1),
    )

    result = await _repository(session_factory, run_id).start_answer_generation()

    run = await _load_run(session_factory, run_id)
    assert result == AnswerGenerationStarted(run_id, run.answer_started_at)
    assert run.answer_started_at is not None


@pytest.mark.asyncio
async def test_window_stops_stale_attempt_without_writing(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """窓口から古い世代が回答を開始すると、別の試行が担当していることを止まった理由として送出する。"""
    run_id = await _seed_running(
        session_factory,
        deadline_at=await _database_time(session_factory) + timedelta(minutes=1),
    )

    with pytest.raises(AnswerGenerationStopped) as stopped:
        await _repository(
            session_factory, run_id, attempt_epoch=_ATTEMPT_EPOCH - 1
        ).start_answer_generation()

    run = await _load_run(session_factory, run_id)
    assert stopped.value.stopped == AttemptSuperseded()
    assert run.answer_started_at is None


@pytest.mark.asyncio
async def test_window_expires_run_and_notifies_attempt_after_commit(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """窓口から期限後に回答を開始すると、Runを期限切れで閉じてその世代へ知らせ、期限切れを送出する。"""
    run_id = await _seed_running(
        session_factory,
        deadline_at=await _database_time(session_factory) - timedelta(seconds=1),
    )
    publisher = _RecordingPublisher()

    with pytest.raises(AnswerGenerationStopped) as stopped:
        await _repository(
            session_factory, run_id, publisher=publisher
        ).start_answer_generation()

    run = await _load_run(session_factory, run_id)
    assert stopped.value.stopped == DeadlineExceeded()
    assert run.status == "deadline_exceeded"
    assert [
        (published_run_id, epoch, event.model_dump(mode="json", by_alias=True))
        for published_run_id, epoch, event in publisher.events
    ] == [
        (
            run_id,
            _ATTEMPT_EPOCH,
            {"type": "terminal", "status": "deadline_exceeded", "errorCode": None},
        )
    ]


@pytest.mark.asyncio
async def test_lock_wait_uses_database_time_after_acquiring_run_lock(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """行ロックを待つ間に期限を越えた回答開始は、ロック後のDB時刻で期限切れとして終える。"""
    run_id = await _seed_running(session_factory)

    async with (
        session_factory() as locker,
        session_factory() as contender,
        session_factory() as observer,
    ):
        repository = AgentRunStateRepository(contender)
        start_task: asyncio.Task[AnswerGenerationStarted | CannotContinue] | None = None
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
                repository.start_answer_generation(
                    run_id,
                    attempt_epoch=_ATTEMPT_EPOCH,
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
    assert (result, repository.closed_runs) == (
        DeadlineExceeded(),
        _closed_by_deadline(run_id),
    )
    assert run.status == "deadline_exceeded"
    assert run.answer_started_at is None


@pytest.mark.asyncio
async def test_authorize_before_deadline_keeps_start_time(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """受理期限前の再生成は続けてよいと返し、開始時刻を変えない。"""
    run_id = await _seed_running(
        session_factory,
        answer_started_at=_ANSWER_STARTED_AT,
    )

    result = await _authorize_at(session_factory, run_id, now=_BEFORE_DEADLINE)

    run = await _load_run(session_factory, run_id)
    assert result == (CanContinue(), [])
    assert run.status == "running"
    assert run.answer_started_at == _ANSWER_STARTED_AT


@pytest.mark.asyncio
async def test_authorize_at_deadline_expires_without_changing_start_time(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """受理期限ちょうどの再生成はRunを期限切れで終えてその世代へ知らせ、開始時刻を変えない。"""
    run_id = await _seed_running(
        session_factory,
        answer_started_at=_ANSWER_STARTED_AT,
    )

    result = await _authorize_at(session_factory, run_id, now=_DEADLINE_AT)

    run = await _load_run(session_factory, run_id)
    assert result == (DeadlineExceeded(), _closed_by_deadline(run_id))
    assert run.status == "deadline_exceeded"
    assert run.answer_started_at == _ANSWER_STARTED_AT


@pytest.mark.asyncio
async def test_authorize_without_start_does_not_expire(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """回答を開始していない試行の再生成は期限より先に呼び出し順の誤りとして例外になり、期限切れにしない。"""
    run_id = await _seed_running(session_factory)

    with pytest.raises(RunPhaseMismatchError):
        await _authorize_at(session_factory, run_id, now=_DEADLINE_AT)

    run = await _load_run(session_factory, run_id)
    assert run.status == "running"
    assert run.answer_started_at is None


@pytest.mark.asyncio
async def test_authorize_stale_attempt_does_not_change_run(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """古い世代の窓口からの再生成は、期限より先に別の試行が担当していることを送出し、Runを変えない。"""
    run_id = await _seed_running(
        session_factory,
        answer_started_at=_ANSWER_STARTED_AT,
    )

    with pytest.raises(AnswerGenerationStopped) as stopped:
        await _repository(
            session_factory, run_id, attempt_epoch=_ATTEMPT_EPOCH - 1
        ).authorize_answer_regeneration()

    run = await _load_run(session_factory, run_id)
    assert stopped.value.stopped == AttemptSuperseded()
    assert run.status == "running"
    assert run.answer_started_at == _ANSWER_STARTED_AT


@pytest.mark.asyncio
async def test_start_commit_failure_returns_no_success(session_factory):
    # commitに失敗した開始記録を予約元へ成功として渡さない。
    from sqlalchemy import event

    run_id = await _seed_running(
        session_factory,
        deadline_at=await _database_time(session_factory) + timedelta(minutes=1),
    )
    session = session_factory()

    def reject_commit(_session):
        raise RuntimeError("commit rejected")

    event.listen(session.sync_session, "before_commit", reject_commit, once=True)
    repository = _repository(lambda: session, run_id)  # type: ignore[arg-type]
    with pytest.raises(RuntimeError, match="commit rejected"):
        await repository.start_answer_generation()
    run = await _load_run(session_factory, run_id)
    assert run.answer_started_at is None
    assert run.status == "running"


@pytest.mark.asyncio
async def test_started_time_schedules_recovery_after_original_deadline(session_factory):
    # 55秒の開始記録から予約し、元の60秒では維持、生成後の期限で回収する。
    from app.agent.answering.timing import answer_generation_recovery_window
    from app.agent.running.deadline.scheduling import AgentDeadlineScheduler
    from tests.agent.run_deadline.clock import sweep_deadline_exceeded_runs_at
    from tests.agent.run_deadline.test_scheduling import RecordingSource

    run_id = await _seed_running(session_factory)
    started_at = _DEADLINE_AT - timedelta(seconds=5)
    result, _closed = await _start_at(session_factory, run_id, now=started_at)
    assert isinstance(result, AnswerGenerationStarted)
    source = RecordingSource()
    scheduler = AgentDeadlineScheduler(source)
    recovery_at = result.answer_started_at + answer_generation_recovery_window()
    scheduler.reserve_in_background(result.run_id, recovery_at)
    await asyncio.gather(*scheduler._pending)
    assert source.schedules[0].time == recovery_at
    assert source.schedules[0].args == [{"run_id": str(run_id)}]
    for at, expected in [(_DEADLINE_AT, 0), (recovery_at, 1)]:
        async with session_factory() as session:
            async with session.begin():
                recovered = await sweep_deadline_exceeded_runs_at(session, at=at)
        assert recovered.total_count == expected
