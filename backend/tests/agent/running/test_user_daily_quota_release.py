"""日次quotaの返却契約。

実DBに seed した Run と利用枠に対して AgentRunStateService の操作で確かめる。
競合は行ロックの待ちと commit の足止めで再現し、終了通知は記録する偽物で受ける。
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import DateTime, Select, literal, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent.contract import AnswerPlanSummary, AnswerQuestionResult
from app.agent.running.daily_quota.acquire import (
    _build_acquire_daily_usage_statement,
)
from app.agent.running.deadline.policy import RUN_DEADLINE_SECONDS
from app.agent.running.state import (
    CancelledByUser,
    CannotContinue,
    RunCompleted,
    RunFailed,
    RunNotFound,
)
from app.agent.running.state_repository import AgentRunStateRepository
from app.agent.running.types import AgentRunErrorCode, AgentRunStatus, RunAttempt
from app.models.agent_message import AgentMessage
from app.models.agent_run import AgentRun
from app.models.agent_thread import AgentThread
from app.models.agent_user_daily_quota import AgentUserDailyQuota
from tests.agent.running._start_run_outcomes import started_attempt_epoch
from tests.agent.running._state_service import (
    ClosedRunningRun,
    ClosedRunNotices,
    CommitGate,
    state_service,
    wait_until_lock_waiting,
)
from tests.conftest import TEST_ADMIN_ID, TEST_USER_ID

pytestmark = pytest.mark.integration

_USER_ID = uuid.UUID(TEST_USER_ID)
_ADMIN_ID = uuid.UUID(TEST_ADMIN_ID)
_USAGE_DATE = date(2026, 7, 19)
_CURRENT_DATE = date(2026, 7, 20)
_USAGE_CLOCK = datetime(2026, 7, 19, 3, 0, tzinfo=UTC)
_NOW = datetime(2026, 7, 20, 12, 0, tzinfo=UTC)
_FENCE_DEADLINE_AT = _NOW + timedelta(seconds=60)


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
    deadline_at: datetime | None = None,
    answer_started_at: datetime | None = None,
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

        effective_created_at = created_at or datetime.now(UTC)
        run = AgentRun(
            thread_id=thread.id,
            user_message_id=user_message.id,
            assistant_message_id=assistant_message_id,
            status=status,
            error_code="internal_error" if status == "failed" else None,
            created_at=effective_created_at,
            deadline_at=(
                deadline_at
                if deadline_at is not None
                else effective_created_at + timedelta(seconds=RUN_DEADLINE_SECONDS)
            ),
            answer_started_at=answer_started_at,
            attempt_epoch=attempt_epoch,
            quota_usage_date=quota_usage_date,
        )
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
) -> tuple[object, list[ClosedRunningRun]]:
    """キャンセルの結果と、そのキャンセルが実行中だった試行へ送った終了通知を返す。"""
    notices = ClosedRunNotices()
    result = await state_service(session_factory, notices=notices).cancel_for_user(
        seeded.run_id,
        user_id=user_id or seeded.user_id,
    )
    return result, notices.closed_runs


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


def _reservation_statement(
    user_id: uuid.UUID,
) -> Select[tuple[datetime, date, datetime, int | None]]:
    return _build_acquire_daily_usage_statement(
        user_id=user_id,
        clock_expression=literal(_USAGE_CLOCK, type_=DateTime(timezone=True)),
    )


def _completed_result() -> AnswerQuestionResult:
    return AnswerQuestionResult(
        status="answered",
        answer="completed quota run",
        sources=[],
        missing_aspects=[],
        plan_summary=AnswerPlanSummary(plan_type="direct_answer"),
    )


@pytest.mark.asyncio
async def test_quota_queued_cancel_releases_to_zero_and_keeps_marker_and_row(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """queuedのRunのキャンセルは利用枠を0まで返し、Runの利用日と枠の行は残す。"""
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
    assert result == (
        CancelledByUser(),
        [],
    )


@pytest.mark.asyncio
async def test_double_cancel_refunds_once_and_second_cancel_changes_nothing(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """同じRunを2回キャンセルしても返却は1回だけで、2回目は続けられないと返り何も閉じない。"""
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
    assert first == (
        CancelledByUser(),
        [],
    )
    assert second == (CannotContinue(), [])


@pytest.mark.asyncio
async def test_running_quota_cancel_is_not_eligible_and_returns_execution_epoch(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """runningのRunのキャンセルは利用枠を返さず、終了通知の送り先になる実行世代を記録する。"""
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
    assert result == (
        CancelledByUser(),
        [
            ClosedRunningRun(
                attempt=RunAttempt(run_id=seeded.run_id, epoch=1),
                status=AgentRunStatus.FAILED,
                error_code=AgentRunErrorCode.CANCELLED,
            )
        ],
    )


@pytest.mark.asyncio
async def test_quota_queued_start_at_deadline_keeps_reservation(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """受理期限ちょうどに開始しようとしたqueuedのRunは開始せず、利用枠を返さない。"""
    seeded = await _seed_run(
        session_factory,
        created_at=_NOW,
        deadline_at=_FENCE_DEADLINE_AT,
    )

    await state_service(session_factory, now=_FENCE_DEADLINE_AT).start_attempt(
        seeded.run_id
    )

    assert (
        await _read_counter(
            session_factory,
            user_id=_USER_ID,
            usage_date=_USAGE_DATE,
        )
        == 1
    )


@pytest.mark.asyncio
async def test_quota_running_start_at_deadline_keeps_reservation(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """受理期限ちょうどに再配信されたrunningのRunは開始せず、利用枠を返さない。"""
    seeded = await _seed_run(
        session_factory,
        status="running",
        attempt_epoch=4,
        created_at=_NOW,
        deadline_at=_FENCE_DEADLINE_AT,
    )

    await state_service(session_factory, now=_FENCE_DEADLINE_AT).start_attempt(
        seeded.run_id
    )

    assert (
        await _read_counter(
            session_factory,
            user_id=_USER_ID,
            usage_date=_USAGE_DATE,
        )
        == 1
    )


@pytest.mark.asyncio
async def test_legacy_queued_cancel_returns_no_quota_without_counter(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """利用日を持たない旧来のqueuedのRunは、キャンセルしても利用枠を返却しない。"""
    seeded = await _seed_run(
        session_factory,
        quota_usage_date=None,
        counter_count=None,
    )

    result = await _cancel(session_factory, seeded)

    assert result == (
        CancelledByUser(),
        [],
    )


@pytest.mark.parametrize("counter_count", [None, 0])
@pytest.mark.asyncio
async def test_missing_or_zero_counter_cancels_without_underflow(
    session_factory: async_sessionmaker[AsyncSession],
    counter_count: int | None,
) -> None:
    """利用枠の行が無いか0のときは、キャンセルは成立し、利用回数を負にしない。"""
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
    assert result == (
        CancelledByUser(),
        [],
    )


@pytest.mark.asyncio
async def test_other_user_cannot_cancel_and_owner_releases_only_original_date(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """他のユーザーはキャンセルできず、持ち主のキャンセルはRunの利用日の枠だけを返す。"""
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

    assert denied == (RunNotFound(), [])
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
    assert released == (
        CancelledByUser(),
        [],
    )


@pytest.mark.asyncio
async def test_cancel_winner_refunds_before_waiting_start_loses(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """先にロックを取ったキャンセルが利用枠を返し、ロックを待っていた開始は書き換え後のRunを見て開始しない。"""
    seeded = await _seed_run(session_factory)
    notices = ClosedRunNotices()
    cancel_gate = CommitGate(session_factory)

    async with session_factory() as observer:
        cancel_task = None
        start_task = None
        try:
            cancel_task = asyncio.create_task(
                state_service(cancel_gate, notices=notices).cancel_for_user(  # type: ignore[arg-type]
                    seeded.run_id,
                    user_id=_USER_ID,
                )
            )
            await asyncio.wait_for(cancel_gate.reached.wait(), timeout=5)

            start_task = asyncio.create_task(
                state_service(session_factory, notices=notices).start_attempt(
                    seeded.run_id
                )
            )
            await wait_until_lock_waiting(observer)

            cancel_gate.open()
            cancel_result = await asyncio.wait_for(cancel_task, timeout=5)
            start_result = await asyncio.wait_for(start_task, timeout=5)
        finally:
            cancel_gate.open()
            for task in (cancel_task, start_task):
                if task is not None:
                    if not task.done():
                        task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
            if observer.in_transaction():
                await observer.rollback()

    assert start_result == CannotContinue()
    assert (
        await _read_counter(
            session_factory,
            user_id=_USER_ID,
            usage_date=_USAGE_DATE,
        )
        == 0
    )
    assert (cancel_result, notices.closed_runs) == (
        CancelledByUser(),
        [],
    )


@pytest.mark.asyncio
async def test_cancel_waiting_on_run_lock_uses_winning_status_update_for_release(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """ロックを待つ間にRunが開始されたら、キャンセルはロック後のrunningとして終え利用枠を返さない。"""
    seeded = await _seed_run(session_factory)
    notices = ClosedRunNotices()
    start_gate = CommitGate(session_factory)

    async with session_factory() as observer:
        start_task = None
        cancel_task = None
        try:
            start_task = asyncio.create_task(
                state_service(start_gate, notices=notices).start_attempt(  # type: ignore[arg-type]
                    seeded.run_id
                )
            )
            await asyncio.wait_for(start_gate.reached.wait(), timeout=5)

            cancel_task = asyncio.create_task(
                state_service(session_factory, notices=notices).cancel_for_user(
                    seeded.run_id,
                    user_id=_USER_ID,
                )
            )
            await wait_until_lock_waiting(observer)

            start_gate.open()
            attempt_epoch = started_attempt_epoch(
                await asyncio.wait_for(start_task, timeout=5)
            )
            assert attempt_epoch == 1

            cancel_result = await asyncio.wait_for(cancel_task, timeout=5)
        finally:
            start_gate.open()
            for task in (start_task, cancel_task):
                if task is not None:
                    if not task.done():
                        task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
            if observer.in_transaction():
                await observer.rollback()

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
    assert (cancel_result, notices.closed_runs) == (
        CancelledByUser(),
        [
            ClosedRunningRun(
                attempt=RunAttempt(run_id=seeded.run_id, epoch=1),
                status=AgentRunStatus.FAILED,
                error_code=AgentRunErrorCode.CANCELLED,
            )
        ],
    )


@pytest.mark.asyncio
async def test_waiting_deadline_sweep_does_not_overwrite_cancel(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """回収がロックを待つ間に先に終えたキャンセルを、回収は上書きせず二重に返さない。"""
    seeded = await _seed_run(
        session_factory,
        created_at=_NOW - timedelta(seconds=61),
        deadline_at=_NOW - timedelta(seconds=1),
    )
    notices = ClosedRunNotices()
    winner_gate = CommitGate(session_factory)
    # 期限を過ぎたキャンセルは書かずに止まるため、受理期限の前に行う。
    winner = state_service(
        winner_gate,  # type: ignore[arg-type]
        notices=notices,
        now=_NOW - timedelta(seconds=2),
    ).cancel_for_user(seeded.run_id, user_id=_USER_ID)

    async with (
        session_factory() as sweep_session,
        session_factory() as observer,
    ):
        winner_task = None
        sweep_task = None
        try:
            winner_task = asyncio.create_task(winner)
            await asyncio.wait_for(winner_gate.reached.wait(), timeout=5)

            await sweep_session.begin()
            sweep_pid = await sweep_session.scalar(text("SELECT pg_backend_pid()"))
            assert isinstance(sweep_pid, int)
            sweep_task = asyncio.create_task(
                AgentRunStateRepository(
                    sweep_session, now=_NOW
                ).end_runs_past_deadline()
            )
            await _wait_until_blocked(observer, sweep_pid)

            winner_gate.open()
            winner_result = await asyncio.wait_for(winner_task, timeout=5)
            sweep_result = await asyncio.wait_for(sweep_task, timeout=5)
            await sweep_session.commit()
        finally:
            winner_gate.open()
            for task in (winner_task, sweep_task):
                if task is not None:
                    if not task.done():
                        task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
            for session in (sweep_session, observer):
                if session.in_transaction():
                    await session.rollback()

    assert (winner_result, notices.closed_runs) == (
        CancelledByUser(),
        [],
    )
    assert sweep_result == ()
    async with session_factory() as verification:
        run = await verification.get(AgentRun, seeded.run_id)
        assert run is not None
        assert (run.status, run.error_code, run.attempt_epoch) == (
            "failed",
            "cancelled",
            0,
        )
    assert (
        await _read_counter(
            session_factory,
            user_id=_USER_ID,
            usage_date=_USAGE_DATE,
        )
        == 0
    )


@pytest.mark.asyncio
async def test_record_failure_never_refunds(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """runningのRunの失敗記録は利用枠を返さず、その後のキャンセルも失敗済みと返り何も閉じない。"""
    seeded = await _seed_run(
        session_factory,
        status="running",
        attempt_epoch=3,
    )
    transition_notices = ClosedRunNotices()

    recorded = await state_service(
        session_factory, notices=transition_notices
    ).end_run_after_attempt_failure(
        RunAttempt(run_id=seeded.run_id, epoch=3),
        error_code=AgentRunErrorCode.INTERNAL_ERROR,
    )
    assert (recorded, transition_notices.closed_runs) == (
        RunFailed(),
        [
            ClosedRunningRun(
                attempt=RunAttempt(run_id=seeded.run_id, epoch=3),
                status=AgentRunStatus.FAILED,
                error_code=AgentRunErrorCode.INTERNAL_ERROR,
            )
        ],
    )

    result = await _cancel(session_factory, seeded)

    assert (
        await _read_counter(
            session_factory,
            user_id=_USER_ID,
            usage_date=_USAGE_DATE,
        )
        == 1
    )
    assert result == (CannotContinue(), [])


@pytest.mark.parametrize(
    ("transition", "status", "attempt_epoch", "expected_counter"),
    [
        ("enqueue_failed", "queued", 0, 0),
        ("complete", "running", 1, 1),
    ],
)
@pytest.mark.asyncio
async def test_competing_terminal_transition_wins_without_refund(
    session_factory: async_sessionmaker[AsyncSession],
    transition: str,
    status: str,
    attempt_epoch: int,
    expected_counter: int,
) -> None:
    """待っていたキャンセルより先に別の操作がRunを終えたら、キャンセルは続けられないと返し、返却は先の操作の分だけになる。"""
    seeded = await _seed_run(
        session_factory,
        status=status,
        attempt_epoch=attempt_epoch,
        answer_started_at=_NOW if transition == "complete" else None,
    )
    notices = ClosedRunNotices()
    transition_gate = CommitGate(session_factory)

    async with session_factory() as observer:
        transition_task = None
        cancel_task = None
        try:
            if transition == "enqueue_failed":
                transition_task = asyncio.create_task(
                    state_service(
                        transition_gate,  # type: ignore[arg-type]
                        notices=notices,
                    ).end_run_after_enqueue_failure(seeded.run_id)
                )
                await asyncio.wait_for(transition_gate.reached.wait(), timeout=5)
            else:
                transition_task = asyncio.create_task(
                    state_service(
                        transition_gate,  # type: ignore[arg-type]
                        notices=notices,
                        now=_NOW,
                    ).complete(
                        RunAttempt(run_id=seeded.run_id, epoch=1),
                        result=_completed_result(),
                        research_handoff=None,
                    )
                )
                await asyncio.wait_for(transition_gate.reached.wait(), timeout=5)

            # 条件を満たさないキャンセルは待たないので、先の操作と同じ時刻で呼ぶ。
            cancel_task = asyncio.create_task(
                state_service(
                    session_factory, notices=notices, now=_NOW
                ).cancel_for_user(seeded.run_id, user_id=_USER_ID)
            )
            await wait_until_lock_waiting(observer)

            if transition == "enqueue_failed":
                assert transition_task is not None
                transition_gate.open()
                async with asyncio.timeout(5):
                    recorded = await transition_task
                assert recorded == RunFailed()
                expected_closed = []
            else:
                assert transition_task is not None
                transition_gate.open()
                async with asyncio.timeout(5):
                    completed = await transition_task
                assert completed == RunCompleted()
                expected_closed = [
                    ClosedRunningRun(
                        attempt=RunAttempt(run_id=seeded.run_id, epoch=1),
                        status=AgentRunStatus.COMPLETED,
                        error_code=None,
                    )
                ]

            cancel_result = await asyncio.wait_for(cancel_task, timeout=5)
        finally:
            transition_gate.open()
            for task in (transition_task, cancel_task):
                if task is not None:
                    if not task.done():
                        task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
            if observer.in_transaction():
                await observer.rollback()

    assert (
        await _read_counter(
            session_factory,
            user_id=_USER_ID,
            usage_date=_USAGE_DATE,
        )
        == expected_counter
    )
    assert (cancel_result, notices.closed_runs) == (CannotContinue(), expected_closed)


@pytest.mark.parametrize("status", ["completed", "failed"])
@pytest.mark.asyncio
async def test_terminal_cancel_returns_no_quota(
    session_factory: async_sessionmaker[AsyncSession],
    status: str,
) -> None:
    """終了済みのRunのキャンセルは続けられないと返すだけで何も閉じず、利用枠を返さない。"""
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
    assert result == (CannotContinue(), [])


@pytest.mark.parametrize("first_winner", ["release", "reserve_rejection"])
@pytest.mark.asyncio
async def test_release_and_reserve_linearize_in_quota_row_lock_order(
    session_factory: async_sessionmaker[AsyncSession],
    first_winner: str,
) -> None:
    """キャンセルによる返却と予約は利用枠の行ロックの順に直列化され、後の側は先の側の結果を見る。"""
    seeded = await _seed_run(session_factory, counter_count=10)
    notices = ClosedRunNotices()
    release_gate = CommitGate(session_factory)

    async with (
        session_factory() as reservation_session,
        session_factory() as observer,
    ):
        release_task = None
        reservation_task = None
        try:
            if first_winner == "release":
                release_task = asyncio.create_task(
                    state_service(
                        release_gate,  # type: ignore[arg-type]
                        notices=notices,
                    ).cancel_for_user(seeded.run_id, user_id=_USER_ID)
                )
                await asyncio.wait_for(release_gate.reached.wait(), timeout=5)

                await reservation_session.begin()
                reservation_pid = await reservation_session.scalar(
                    text("SELECT pg_backend_pid()")
                )
                assert isinstance(reservation_pid, int)
                reservation_task = asyncio.create_task(
                    reservation_session.execute(_reservation_statement(_USER_ID))
                )
                await _wait_until_blocked(observer, reservation_pid)

                release_gate.open()
                release_result = await asyncio.wait_for(release_task, timeout=5)
                reservation_result = await asyncio.wait_for(
                    reservation_task,
                    timeout=5,
                )
                await reservation_session.commit()
                reservation = reservation_result.mappings().one()
                assert reservation["used_count"] == 10
                expected_count = 10
            else:
                await reservation_session.begin()
                rejection = (
                    (
                        await reservation_session.execute(
                            _reservation_statement(_USER_ID)
                        )
                    )
                    .mappings()
                    .one()
                )
                assert rejection["used_count"] is None

                release_task = asyncio.create_task(
                    state_service(session_factory, notices=notices).cancel_for_user(
                        seeded.run_id,
                        user_id=_USER_ID,
                    )
                )
                await wait_until_lock_waiting(observer)

                await reservation_session.commit()
                release_result = await asyncio.wait_for(release_task, timeout=5)
                expected_count = 9
        finally:
            release_gate.open()
            for task in (release_task, reservation_task):
                if task is not None:
                    if not task.done():
                        task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
            for session in (reservation_session, observer):
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
    assert (release_result, notices.closed_runs) == (
        CancelledByUser(),
        [],
    )
