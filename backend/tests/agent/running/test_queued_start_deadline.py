"""queued Run の受理期限による開始の禁止と利用枠の返却。

実DBで AgentRunStateService の操作から確かめ、返却の観測は記録に差し替える。
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast

import pytest
from logfire.testing import CaptureLogfire
from sqlalchemy import event as sa_event
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import app.agent.run_execution as run_execution
import app.queue.tasks.agent_run as agent_run_tasks
from app.agent.run_enqueuer import AgentRunTrigger
from app.agent.running.daily_quota.release import DailyQuotaReleaseOutcome
from app.agent.running.deadline.policy import RUN_DEADLINE_SECONDS
from app.agent.running.finish_notice import ClosedQueuedRun
from app.agent.running.state import (
    AttemptStarted,
    CancelledByUser,
    CannotContinue,
    DeadlineExceeded,
    RunAlreadyCancelled,
)
from app.agent.running.types import RunAttempt
from app.models.agent_message import AgentMessage
from app.models.agent_run import AgentRun
from app.models.agent_thread import AgentThread
from app.models.agent_user_daily_quota import AgentUserDailyQuota
from tests.agent._sensitive_db_failure import assert_no_sensitive_marker
from tests.agent.running._state_service import (
    CommitGate,
    record_closed_runs,
    state_service,
    wait_until_lock_waiting,
)
from tests.conftest import TEST_USER_ID
from tests.logfire._metric_helpers import collected_metrics

_USER_ID = uuid.UUID(TEST_USER_ID)
_USAGE_DATE = date(2026, 7, 22)
_DB_NOW = datetime(2026, 7, 22, 12, 0, tzinfo=UTC)
_SENSITIVE_START_ERROR = (
    "asyncpg failure\n"
    "DETAIL:  Key (question)=(SECRET_QUESTION_MARKER) SECRET_SQL_MARKER"
)


class _SensitiveQuotaQueryFailure(DBAPIError):
    """秘匿値をSQLのパラメータとDETAILに持つ、日次上限の更新時のDB例外。"""

    def __init__(self) -> None:
        super().__init__(
            "UPDATE agent_user_daily_quotas SET used_count = $1 WHERE user_id = $2",
            {
                "user_id": str(_USER_ID),
                "question": "SECRET_QUESTION_MARKER",
                "answer": "SECRET_ANSWER_MARKER",
                "provider_raw": "SECRET_PROVIDER_RAW_MARKER",
            },
            RuntimeError(_SENSITIVE_START_ERROR),
        )


@dataclass(frozen=True, slots=True)
class _SeededRun:
    run_id: uuid.UUID
    user_id: uuid.UUID
    quota_usage_date: date | None


async def _seed_queued_run(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    created_at: datetime,
    quota_usage_date: date | None = _USAGE_DATE,
    counter_count: int | None = 1,
    status: str = "queued",
    attempt_epoch: int = 0,
) -> _SeededRun:
    async with session_factory() as session:
        if counter_count is not None:
            assert quota_usage_date is not None
            session.add(
                AgentUserDailyQuota(
                    user_id=_USER_ID,
                    usage_date=quota_usage_date,
                    used_count=counter_count,
                )
            )
        thread = AgentThread(user_id=_USER_ID, title="queued deadline")
        session.add(thread)
        await session.flush()
        message = AgentMessage(
            thread_id=thread.id,
            seq=1,
            role="user",
            content="queued deadline question",
            missing_aspects=[],
        )
        session.add(message)
        await session.flush()
        run = AgentRun(
            thread_id=thread.id,
            user_message_id=message.id,
            status=status,
            created_at=created_at,
            deadline_at=created_at + timedelta(seconds=RUN_DEADLINE_SECONDS),
            attempt_epoch=attempt_epoch,
            quota_usage_date=quota_usage_date,
        )
        session.add(run)
        await session.commit()
        return _SeededRun(
            run_id=run.id,
            user_id=_USER_ID,
            quota_usage_date=quota_usage_date,
        )


async def _read_run(
    session_factory: async_sessionmaker[AsyncSession], run_id: uuid.UUID
) -> AgentRun:
    async with session_factory() as session:
        run = await session.get(AgentRun, run_id)
    assert run is not None
    return run


async def _read_counter(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    usage_date: date,
) -> int | None:
    async with session_factory() as session:
        return await session.scalar(
            select(AgentUserDailyQuota.used_count).where(
                AgentUserDailyQuota.user_id == _USER_ID,
                AgentUserDailyQuota.usage_date == usage_date,
            )
        )


def _run_deadline_seconds() -> int:
    value = RUN_DEADLINE_SECONDS
    assert value == 60
    return value


def _ctx(session_factory: async_sessionmaker[AsyncSession]) -> SimpleNamespace:
    return SimpleNamespace(
        state=SimpleNamespace(
            session_factory=session_factory,
            agent_live_redis=object(),
            agent_deadline_scheduler=SimpleNamespace(
                reserve_in_background=lambda *_: None
            ),
        )
    )


def _quota_release_metric_points(capfire: CaptureLogfire) -> list[dict[str, Any]]:
    metric = next(
        (
            item
            for item in collected_metrics(capfire)
            if item["name"] == "agent_user_daily_quota_releases_total"
        ),
        None,
    )
    if metric is None:
        return []
    return list(metric["data"]["data_points"])


@pytest.mark.integration
@pytest.mark.asyncio
async def test_expired_queued_run_terminalizes_and_releases_original_quota_atomically(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """期限を過ぎたqueuedのRunの開始は、期限切れで終えると同時に予約した利用枠を返す。"""
    deadline_seconds = _run_deadline_seconds()
    seeded = await _seed_queued_run(
        session_factory,
        created_at=_DB_NOW
        - timedelta(
            seconds=deadline_seconds,
            microseconds=1,
        ),
    )
    notices = record_closed_runs(monkeypatch)

    result = await state_service(
        session_factory, notices=notices, now=_DB_NOW
    ).start_attempt(seeded.run_id)

    assert (result, notices.closed_runs) == (
        DeadlineExceeded(),
        [
            ClosedQueuedRun(
                run_id=seeded.run_id,
                quota_release_outcome=DailyQuotaReleaseOutcome.RELEASED,
            )
        ],
    )
    run = await _read_run(session_factory, seeded.run_id)
    assert (run.status, run.error_code, run.attempt_epoch) == (
        "deadline_exceeded",
        None,
        0,
    )
    assert await _read_counter(session_factory, usage_date=_USAGE_DATE) == 0


@pytest.mark.integration
@pytest.mark.asyncio
async def test_queued_start_deadline_starts_just_before_and_expires_at_boundary(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """期限の直前は開始し、期限ちょうどは期限切れで終える。"""
    deadline_seconds = _run_deadline_seconds()
    just_before = await _seed_queued_run(
        session_factory,
        created_at=_DB_NOW
        - timedelta(seconds=deadline_seconds)
        + timedelta(microseconds=1),
    )
    exact = await _seed_queued_run(
        session_factory,
        created_at=_DB_NOW - timedelta(seconds=deadline_seconds),
        quota_usage_date=date(2026, 7, 21),
    )
    notices = record_closed_runs(monkeypatch)
    service = state_service(session_factory, notices=notices, now=_DB_NOW)

    before_result = await service.start_attempt(just_before.run_id)
    exact_result = await service.start_attempt(exact.run_id)

    assert before_result == AttemptStarted(
        attempt=RunAttempt(run_id=just_before.run_id, epoch=1)
    )
    assert (exact_result, notices.closed_runs) == (
        DeadlineExceeded(),
        [
            ClosedQueuedRun(
                run_id=exact.run_id,
                quota_release_outcome=DailyQuotaReleaseOutcome.RELEASED,
            )
        ],
    )
    before_run = await _read_run(session_factory, just_before.run_id)
    exact_run = await _read_run(session_factory, exact.run_id)
    assert (before_run.status, exact_run.status) == (
        "running",
        "deadline_exceeded",
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_expired_legacy_queued_run_terminalizes_without_quota_release(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """利用枠を予約していない旧来のqueuedのRunは、期限切れで終えても利用枠を返さない。"""
    seeded = await _seed_queued_run(
        session_factory,
        created_at=_DB_NOW - timedelta(seconds=RUN_DEADLINE_SECONDS + 1),
        quota_usage_date=None,
        counter_count=None,
    )
    notices = record_closed_runs(monkeypatch)

    result = await state_service(
        session_factory, notices=notices, now=_DB_NOW
    ).start_attempt(seeded.run_id)

    assert (result, notices.closed_runs) == (
        DeadlineExceeded(),
        [
            ClosedQueuedRun(
                run_id=seeded.run_id,
                quota_release_outcome=DailyQuotaReleaseOutcome.NOT_ELIGIBLE,
            )
        ],
    )
    run = await _read_run(session_factory, seeded.run_id)
    assert (run.status, run.error_code) == ("deadline_exceeded", None)


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("counter_count", [None, 0])
async def test_expired_queued_run_with_missing_or_empty_counter_is_inconsistent(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    counter_count: int | None,
) -> None:
    """返す先の利用枠が無いか0なら、期限切れで終えて返却の不整合を返す。"""
    seeded = await _seed_queued_run(
        session_factory,
        created_at=_DB_NOW - timedelta(seconds=RUN_DEADLINE_SECONDS + 1),
        counter_count=counter_count,
    )
    notices = record_closed_runs(monkeypatch)

    result = await state_service(
        session_factory, notices=notices, now=_DB_NOW
    ).start_attempt(seeded.run_id)

    assert (result, notices.closed_runs) == (
        DeadlineExceeded(),
        [
            ClosedQueuedRun(
                run_id=seeded.run_id,
                quota_release_outcome=DailyQuotaReleaseOutcome.INCONSISTENT,
            )
        ],
    )
    run = await _read_run(session_factory, seeded.run_id)
    assert run.status == "deadline_exceeded"
    assert await _read_counter(session_factory, usage_date=_USAGE_DATE) == counter_count


@pytest.mark.integration
@pytest.mark.asyncio
async def test_quota_query_failure_rolls_back_queued_expiry_without_a_committed_outcome(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """利用枠の返却が失敗すると、期限切れの確定ごと巻き戻る。"""
    seeded = await _seed_queued_run(
        session_factory,
        created_at=_DB_NOW - timedelta(seconds=RUN_DEADLINE_SECONDS + 1),
    )
    session = session_factory()

    def fail_quota_update(
        _connection: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: object,
    ) -> None:
        if "UPDATE agent_user_daily_quotas" in statement:
            raise RuntimeError("queued expiry quota query failed")

    sa_event.listen(
        session.sync_session.bind,
        "before_cursor_execute",
        fail_quota_update,
    )

    def failing_session_factory() -> AsyncSession:
        return session

    try:
        with pytest.raises(RuntimeError, match="queued expiry quota query failed"):
            await state_service(
                cast(async_sessionmaker[AsyncSession], failing_session_factory),
                now=_DB_NOW,
            ).start_attempt(seeded.run_id)
    finally:
        sa_event.remove(
            session.sync_session.bind,
            "before_cursor_execute",
            fail_quota_update,
        )
        await session.close()

    run = await _read_run(session_factory, seeded.run_id)
    assert (run.status, run.error_code, run.attempt_epoch) == ("queued", None, 0)
    assert await _read_counter(session_factory, usage_date=_USAGE_DATE) == 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_cancel_winner_refunds_once_and_expired_start_reports_idempotent_skip(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """キャンセルが先に利用枠を返したRunを、期限後に届いた開始は停止済みと見て開始せず、二重に返さない。"""
    seeded = await _seed_queued_run(
        session_factory,
        created_at=_DB_NOW - timedelta(seconds=RUN_DEADLINE_SECONDS + 1),
    )
    # キャンセルも回収期限を見るため、期限より前に届いたキャンセルとして記録する。
    cancelled_at = _DB_NOW - timedelta(seconds=2)
    notices = record_closed_runs(monkeypatch)
    cancel_gate = CommitGate(session_factory)

    async with session_factory() as observer:
        cancel_task: asyncio.Task[object] | None = None
        start_task: asyncio.Task[AttemptStarted | CannotContinue] | None = None
        try:
            cancel_task = asyncio.create_task(
                state_service(
                    cancel_gate,  # type: ignore[arg-type]
                    notices=notices,
                    now=cancelled_at,
                ).cancel_for_user(seeded.run_id, user_id=seeded.user_id)
            )
            await asyncio.wait_for(cancel_gate.reached.wait(), timeout=5)

            start_task = asyncio.create_task(
                state_service(
                    session_factory, notices=notices, now=_DB_NOW
                ).start_attempt(seeded.run_id)
            )
            await wait_until_lock_waiting(observer)

            cancel_gate.open()
            cancelled = await asyncio.wait_for(cancel_task, timeout=5)
            result = await asyncio.wait_for(start_task, timeout=5)
        finally:
            cancel_gate.open()
            for task in (cancel_task, start_task):
                if task is not None:
                    if not task.done():
                        task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
            if observer.in_transaction():
                await observer.rollback()

    assert (cancelled, notices.closed_runs) == (
        CancelledByUser(),
        [
            ClosedQueuedRun(
                run_id=seeded.run_id,
                quota_release_outcome=DailyQuotaReleaseOutcome.RELEASED,
            )
        ],
    )
    assert result == RunAlreadyCancelled()
    run = await _read_run(session_factory, seeded.run_id)
    assert (run.status, run.error_code) == ("failed", "cancelled")
    assert await _read_counter(session_factory, usage_date=_USAGE_DATE) == 0


@pytest.mark.integration
@pytest.mark.asyncio
async def test_timely_start_and_running_redelivery_never_release_quota(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """期限内の開始とrunningの再配信は世代を進めるだけで、利用枠を返さない。"""
    seeded = await _seed_queued_run(
        session_factory,
        created_at=_DB_NOW - timedelta(seconds=RUN_DEADLINE_SECONDS - 2),
    )

    started = await state_service(session_factory, now=_DB_NOW).start_attempt(
        seeded.run_id
    )
    redelivered = await state_service(
        session_factory, now=_DB_NOW + timedelta(seconds=1)
    ).start_attempt(seeded.run_id)

    assert started == AttemptStarted(attempt=RunAttempt(run_id=seeded.run_id, epoch=1))
    assert redelivered == AttemptStarted(
        attempt=RunAttempt(run_id=seeded.run_id, epoch=2)
    )
    run = await _read_run(session_factory, seeded.run_id)
    assert (run.status, run.attempt_epoch, run.error_code) == ("running", 2, None)
    assert await _read_counter(session_factory, usage_date=_USAGE_DATE) == 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_expired_queued_task_skips_live_provider_and_emits_post_commit_telemetry(
    read_logs,
    session_factory: async_sessionmaker[AsyncSession],
    capfire: CaptureLogfire,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seeded = await _seed_queued_run(
        session_factory,
        created_at=datetime.now(UTC) - timedelta(seconds=RUN_DEADLINE_SECONDS + 1),
    )
    forbidden_calls: list[str] = []

    def forbidden(name: str) -> object:
        def raise_if_called(*_args: object, **_kwargs: object) -> None:
            forbidden_calls.append(name)
            raise AssertionError(f"expired queued run must not create {name}")

        return raise_if_called

    monkeypatch.setattr(
        run_execution,
        "AgentRunLiveStreamPublisher",
        forbidden("live stream attempt"),
    )
    monkeypatch.setattr(
        run_execution,
        "AgentRunLiveAnswerDeltaReporter",
        forbidden("answer delta reporter"),
    )
    monkeypatch.setattr(
        run_execution,
        "AgentRunStageGate",
        forbidden("stage gate"),
    )
    monkeypatch.setattr(
        run_execution,
        "AgentRunAnswerGenerationGate",
        forbidden("answer generation gate"),
    )
    monkeypatch.setattr(
        run_execution,
        "build_answering_runner",
        forbidden("answering runner"),
    )

    await agent_run_tasks.run_agent_answer(
        trigger=AgentRunTrigger(run_id=seeded.run_id),
        ctx=_ctx(session_factory),
    )
    logs = read_logs()

    assert forbidden_calls == []
    run = await _read_run(session_factory, seeded.run_id)
    assert (run.status, run.error_code, run.attempt_epoch) == (
        "deadline_exceeded",
        None,
        0,
    )
    assert await _read_counter(session_factory, usage_date=_USAGE_DATE) == 0
    assert [
        entry
        for entry in logs
        if entry.get("event") == "agent_run_start_deadline_exceeded"
    ] == [
        {
            "event": "agent_run_start_deadline_exceeded",
            "level": "info",
            "log_policy": "research",
        }
    ]
    assert {
        (point["value"], frozenset(point.get("attributes", {}).items()))
        for point in _quota_release_metric_points(capfire)
    } == {(1, frozenset({("result", "released")}))}


@pytest.mark.integration
@pytest.mark.asyncio
async def test_queued_expiry_rollback_emits_neither_log_nor_quota_metric(
    read_logs,
    session_factory: async_sessionmaker[AsyncSession],
    capfire: CaptureLogfire,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """期限切れの確定が利用枠の更新の秘匿値つきDB例外で戻ると、taskは例外を出さずログに秘匿値を残さず、Runと利用枠はqueuedのまま残る。"""
    seeded = await _seed_queued_run(
        session_factory,
        created_at=datetime.now(UTC) - timedelta(seconds=RUN_DEADLINE_SECONDS + 1),
    )
    failing_session = session_factory()

    def fail_quota_update(
        _connection: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: object,
    ) -> None:
        if "UPDATE agent_user_daily_quotas" in statement:
            raise _SensitiveQuotaQueryFailure

    sa_event.listen(
        failing_session.sync_session.bind,
        "before_cursor_execute",
        fail_quota_update,
    )

    def failing_session_factory() -> AsyncSession:
        return failing_session

    try:
        await agent_run_tasks.run_agent_answer(
            trigger=AgentRunTrigger(run_id=seeded.run_id),
            ctx=_ctx(
                cast(
                    async_sessionmaker[AsyncSession],
                    failing_session_factory,
                )
            ),
        )
    finally:
        sa_event.remove(
            failing_session.sync_session.bind,
            "before_cursor_execute",
            fail_quota_update,
        )
        await failing_session.close()
    logs = read_logs()

    assert_no_sensitive_marker(logs)
    assert not [
        entry
        for entry in logs
        if entry.get("event") == "agent_run_start_deadline_exceeded"
    ]
    assert _quota_release_metric_points(capfire) == []
    run = await _read_run(session_factory, seeded.run_id)
    assert (run.status, run.error_code, run.attempt_epoch) == ("queued", None, 0)
    assert await _read_counter(session_factory, usage_date=_USAGE_DATE) == 1
