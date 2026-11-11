"""回答開始・再生成許可を AgentRunStateService と回答生成の口から実DBで確かめる。

前提: 時刻を固定する判定は service に時刻を渡して呼び、回答生成の口
(AgentRunAnswerGenerationGate)はDB時刻のまま呼び、予約と通知は記録する偽物で受ける。
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent.answering.timing import answer_generation_recovery_window
from app.agent.live_updates.reporters import AgentRunLiveStageReporter
from app.agent.live_updates.stream import AgentRunLiveStreamEvent
from app.agent.running.answer_generation import AgentRunAnswerGenerationGate
from app.agent.running.finish_notice import ClosedRun, ClosedRunningRun
from app.agent.running.state import (
    AnswerGenerationStarted,
    AttemptSuperseded,
    CanContinue,
    CannotContinue,
    DeadlineExceeded,
    RunAlreadyCancelled,
    RunAlreadyFailed,
    RunNotFound,
    RunPhaseMismatchError,
)
from app.agent.running.state_service import AgentRunStateService
from app.agent.running.types import AgentRunStatus, RunAttempt
from app.models.agent_run import AgentRun
from tests.agent.running._seed import create_thread_message_run
from tests.agent.running._state_service import (
    ClosedRunNotices,
    state_service,
    wait_until_lock_waiting,
)

pytestmark = pytest.mark.integration

_ATTEMPT_EPOCH = 3
_DEADLINE_AT = datetime(2026, 9, 5, 12, 1, tzinfo=UTC)
_BEFORE_DEADLINE = _DEADLINE_AT - timedelta(microseconds=1)
_ANSWER_STARTED_AT = _DEADLINE_AT - timedelta(seconds=5)
# 回答開始から45秒(回答生成15秒 + 猶予30秒)が回収期限なので、受理期限より40秒後になる。
_RECOVERY_DEADLINE_AT = _DEADLINE_AT + timedelta(seconds=40)


class _RecordingPublisher:
    def __init__(self) -> None:
        self.events: list[tuple[uuid.UUID, int, AgentRunLiveStreamEvent]] = []

    def publisher_for(self, attempt: RunAttempt) -> _EpochPublisher:
        return _EpochPublisher(self, attempt.run_id, attempt.epoch)


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


class _ObservedGate:
    """回答生成の口と、その口が送った通知・予約の記録。"""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        run_id: uuid.UUID,
        *,
        attempt_epoch: int = _ATTEMPT_EPOCH,
    ) -> None:
        self.publisher = _RecordingPublisher()
        self.reservations: list[tuple[uuid.UUID, datetime]] = []
        attempt = RunAttempt(run_id=run_id, epoch=attempt_epoch)
        self.gate = AgentRunAnswerGenerationGate(
            AgentRunStateService(
                session_factory=session_factory,
                publisher_for=self.publisher.publisher_for,  # type: ignore[arg-type]
            ),
            attempt,
            schedule_deadline_check=lambda target_run_id, at: self.reservations.append(
                (target_run_id, at)
            ),
            stage_reporter=AgentRunLiveStageReporter(
                self.publisher.publisher_for(attempt)  # type: ignore[arg-type]
            ),
        )

    def published(self) -> list[tuple[uuid.UUID, int, dict[str, Any]]]:
        return [
            (run_id, epoch, event.model_dump(mode="json", by_alias=True))
            for run_id, epoch, event in self.publisher.events
        ]


async def _start_at(
    session_factory: async_sessionmaker[AsyncSession],
    run_id: uuid.UUID,
    *,
    now: datetime,
    attempt_epoch: int = _ATTEMPT_EPOCH,
) -> tuple[AnswerGenerationStarted | CannotContinue, list[ClosedRun]]:
    notices = ClosedRunNotices()
    result = await state_service(
        session_factory, notices=notices, now=now
    ).start_answer_generation(RunAttempt(run_id=run_id, epoch=attempt_epoch))
    return result, notices.closed_runs


async def _authorize_at(
    session_factory: async_sessionmaker[AsyncSession],
    run_id: uuid.UUID,
    *,
    now: datetime,
    attempt_epoch: int = _ATTEMPT_EPOCH,
) -> tuple[CanContinue | CannotContinue, list[ClosedRun]]:
    notices = ClosedRunNotices()
    result = await state_service(
        session_factory, notices=notices, now=now
    ).authorize_answer_regeneration(RunAttempt(run_id=run_id, epoch=attempt_epoch))
    return result, notices.closed_runs


async def _database_time(session_factory: async_sessionmaker[AsyncSession]) -> datetime:
    async with session_factory() as session:
        database_time = await session.scalar(text("SELECT clock_timestamp()"))
    assert isinstance(database_time, datetime)
    return database_time


def _closed_by_deadline(run_id: uuid.UUID) -> list[ClosedRun]:
    return [
        ClosedRunningRun(
            attempt=RunAttempt(run_id=run_id, epoch=_ATTEMPT_EPOCH),
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
    """失敗で終わったRunの回答開始は既に失敗していると返し、状態も開始時刻も書かない。"""
    run_id = await _seed_running(
        session_factory,
        status="failed",
        error_code="internal_error",
    )

    result = await _start_at(session_factory, run_id, now=_BEFORE_DEADLINE)

    run = await _load_run(session_factory, run_id)
    assert result == (RunAlreadyFailed(), [])
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
async def test_gate_start_missing_run_returns_not_found_without_reserving(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """存在しないRunの回答開始は見つからないことを返し、予約も通知もしない。"""
    observed = _ObservedGate(session_factory, uuid.uuid4())

    result = await observed.gate.start()

    assert (result, observed.reservations, observed.published()) == (
        RunNotFound(),
        [],
        [],
    )


@pytest.mark.asyncio
async def test_rolled_back_start_does_not_persist_generation_right(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """巻き戻した回答開始は開始時刻を残さない。"""
    run_id = await _seed_running(session_factory)

    def open_rolling_back_session() -> AsyncSession:
        session = session_factory()
        session.commit = session.rollback  # type: ignore[method-assign]
        return session

    service = state_service(
        open_rolling_back_session,  # type: ignore[arg-type]
        now=_BEFORE_DEADLINE,
    )

    result = await service.start_answer_generation(
        RunAttempt(run_id=run_id, epoch=_ATTEMPT_EPOCH)
    )

    run = await _load_run(session_factory, run_id)
    assert result == AnswerGenerationStarted(run_id, _BEFORE_DEADLINE)
    assert run.answer_started_at is None


@pytest.mark.asyncio
async def test_gate_start_reserves_recovery_from_committed_start_and_notifies_answering(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """回答を始めてよいRunは開始時刻をcommitし、開始時刻に回収の猶予を足した時刻で確認を予約して「回答中」を1件通知する。"""
    run_id = await _seed_running(
        session_factory,
        deadline_at=await _database_time(session_factory) + timedelta(minutes=1),
    )
    observed = _ObservedGate(session_factory, run_id)

    result = await observed.gate.start()

    run = await _load_run(session_factory, run_id)
    assert run.answer_started_at is not None
    assert (result, observed.reservations, observed.published()) == (
        CanContinue(),
        [(run_id, run.answer_started_at + answer_generation_recovery_window())],
        [(run_id, _ATTEMPT_EPOCH, {"type": "stage", "stage": "answering"})],
    )


@pytest.mark.asyncio
async def test_gate_start_stale_attempt_returns_superseded_without_writing(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """古い世代が回答を始めようとすると別の試行が担当していると返し、開始を記録せず予約も通知もしない。"""
    run_id = await _seed_running(
        session_factory,
        deadline_at=await _database_time(session_factory) + timedelta(minutes=1),
    )
    observed = _ObservedGate(session_factory, run_id, attempt_epoch=_ATTEMPT_EPOCH - 1)

    result = await observed.gate.start()

    run = await _load_run(session_factory, run_id)
    assert (result, observed.reservations, observed.published()) == (
        AttemptSuperseded(),
        [],
        [],
    )
    assert run.answer_started_at is None


@pytest.mark.asyncio
async def test_gate_start_cancelled_run_returns_cancelled_without_writing(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """利用者が止めたRunの回答開始は既に止められていると返し、状態も開始時刻も変えず予約も通知もしない。"""
    run_id = await _seed_running(
        session_factory,
        status="failed",
        error_code="cancelled",
        deadline_at=await _database_time(session_factory) + timedelta(minutes=1),
    )
    observed = _ObservedGate(session_factory, run_id)

    result = await observed.gate.start()

    run = await _load_run(session_factory, run_id)
    assert (result, observed.reservations, observed.published()) == (
        RunAlreadyCancelled(),
        [],
        [],
    )
    assert (run.status, run.error_code, run.answer_started_at) == (
        "failed",
        "cancelled",
        None,
    )


@pytest.mark.asyncio
async def test_gate_start_after_deadline_closes_run_and_notifies_only_terminal(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """期限後に回答を始めようとするとRunを期限切れで閉じてその世代へ終了だけを知らせ、期限切れを返して予約しない。"""
    run_id = await _seed_running(
        session_factory,
        deadline_at=await _database_time(session_factory) - timedelta(seconds=1),
    )
    observed = _ObservedGate(session_factory, run_id)

    result = await observed.gate.start()

    run = await _load_run(session_factory, run_id)
    assert (result, observed.reservations, observed.published()) == (
        DeadlineExceeded(),
        [],
        [
            (
                run_id,
                _ATTEMPT_EPOCH,
                {"type": "terminal", "status": "deadline_exceeded", "errorCode": None},
            )
        ],
    )
    assert run.status == "deadline_exceeded"


@pytest.mark.asyncio
async def test_lock_wait_uses_database_time_after_acquiring_run_lock(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """行ロックを待つ間に期限を越えた回答開始は、ロック後のDB時刻で期限切れとして終える。"""
    run_id = await _seed_running(session_factory)

    notices = ClosedRunNotices()
    service = state_service(session_factory, notices=notices)
    async with (
        session_factory() as locker,
        session_factory() as observer,
    ):
        start_task: asyncio.Task[AnswerGenerationStarted | CannotContinue] | None = None
        try:
            await locker.begin()
            locked_run = (
                await locker.execute(
                    select(AgentRun).where(AgentRun.id == run_id).with_for_update()
                )
            ).scalar_one()

            start_task = asyncio.create_task(
                service.start_answer_generation(
                    RunAttempt(run_id=run_id, epoch=_ATTEMPT_EPOCH),
                )
            )
            await wait_until_lock_waiting(observer)

            locked_run.deadline_at = await locker.scalar(
                text("SELECT clock_timestamp()")
            )
            await locker.commit()

            result = await asyncio.wait_for(start_task, timeout=5)
        finally:
            if start_task is not None:
                if not start_task.done():
                    start_task.cancel()
                await asyncio.gather(start_task, return_exceptions=True)
            for session in (locker, observer):
                if session.in_transaction():
                    await session.rollback()

    run = await _load_run(session_factory, run_id)
    assert (result, notices.closed_runs) == (
        DeadlineExceeded(),
        _closed_by_deadline(run_id),
    )
    assert run.status == "deadline_exceeded"
    assert run.answer_started_at is None


@pytest.mark.asyncio
async def test_authorize_before_recovery_deadline_keeps_start_time(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """受理期限を過ぎていても、回収期限の直前の再生成は続けてよいと返し、開始時刻を変えない。"""
    run_id = await _seed_running(
        session_factory,
        answer_started_at=_ANSWER_STARTED_AT,
    )

    result = await _authorize_at(
        session_factory,
        run_id,
        now=_RECOVERY_DEADLINE_AT - timedelta(microseconds=1),
    )

    run = await _load_run(session_factory, run_id)
    assert result == (CanContinue(), [])
    assert run.status == "running"
    assert run.answer_started_at == _ANSWER_STARTED_AT


@pytest.mark.asyncio
async def test_authorize_at_recovery_deadline_expires_without_changing_start_time(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """回収期限ちょうどの再生成はRunを期限切れで終えてその世代へ知らせ、開始時刻を変えない。"""
    run_id = await _seed_running(
        session_factory,
        answer_started_at=_ANSWER_STARTED_AT,
    )

    result = await _authorize_at(session_factory, run_id, now=_RECOVERY_DEADLINE_AT)

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
async def test_gate_regenerate_stale_attempt_returns_superseded_without_change(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """古い世代の書き直しは期限より先に別の試行が担当していると返し、Runを変えない。"""
    run_id = await _seed_running(
        session_factory,
        answer_started_at=_ANSWER_STARTED_AT,
    )
    observed = _ObservedGate(session_factory, run_id, attempt_epoch=_ATTEMPT_EPOCH - 1)

    result = await observed.gate.regenerate()

    run = await _load_run(session_factory, run_id)
    assert (result, observed.published()) == (AttemptSuperseded(), [])
    assert (run.status, run.answer_started_at) == ("running", _ANSWER_STARTED_AT)


@pytest.mark.asyncio
async def test_gate_start_commit_failure_raises_without_reserving(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """開始時刻のcommitに失敗した回答開始は例外を送出し、回収の確認を予約せず「回答中」も通知しない。"""
    from sqlalchemy import event

    run_id = await _seed_running(
        session_factory,
        deadline_at=await _database_time(session_factory) + timedelta(minutes=1),
    )
    session = session_factory()

    def reject_commit(_session: object) -> None:
        raise RuntimeError("commit rejected")

    event.listen(session.sync_session, "before_commit", reject_commit, once=True)
    observed = _ObservedGate(lambda: session, run_id)  # type: ignore[arg-type]

    with pytest.raises(RuntimeError, match="commit rejected"):
        await observed.gate.start()

    run = await _load_run(session_factory, run_id)
    assert (observed.reservations, observed.published()) == ([], [])
    assert (run.status, run.answer_started_at) == ("running", None)


@pytest.mark.asyncio
async def test_gate_start_reservation_recovers_after_original_deadline(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """受理期限の直前に回答を始めたRunは、口が予約した時刻まで回収されず、その時刻に回収される。"""
    from app.agent.running.deadline.scheduling import AgentDeadlineScheduler
    from tests.agent.run_deadline.clock import sweep_deadline_exceeded_runs_at
    from tests.agent.run_deadline.test_scheduling import RecordingSource

    deadline_at = await _database_time(session_factory) + timedelta(seconds=5)
    run_id = await _seed_running(session_factory, deadline_at=deadline_at)
    source = RecordingSource()
    scheduler = AgentDeadlineScheduler(source)
    publisher = _RecordingPublisher()
    attempt = RunAttempt(run_id=run_id, epoch=_ATTEMPT_EPOCH)
    gate = AgentRunAnswerGenerationGate(
        AgentRunStateService(
            session_factory=session_factory,
            publisher_for=publisher.publisher_for,  # type: ignore[arg-type]
        ),
        attempt,
        schedule_deadline_check=scheduler.reserve_in_background,
        stage_reporter=AgentRunLiveStageReporter(
            publisher.publisher_for(attempt)  # type: ignore[arg-type]
        ),
    )

    assert await gate.start() == CanContinue()
    await asyncio.gather(*scheduler._pending)

    [schedule] = source.schedules
    assert schedule.args == [{"run_id": str(run_id)}]
    recovered_counts = []
    for at in (deadline_at, schedule.time):
        async with session_factory() as session:
            async with session.begin():
                recovered = await sweep_deadline_exceeded_runs_at(session, at=at)
        recovered_counts.append(recovered.total_count)
    assert recovered_counts == [0, 1]
