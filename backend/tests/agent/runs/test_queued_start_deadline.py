"""queued start deadline による実行禁止とquota返却の契約。"""

from __future__ import annotations

import asyncio
import inspect
import traceback
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast

import pytest
from logfire.testing import CaptureLogfire
from sqlalchemy import event as sa_event
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from structlog.testing import capture_logs

import app.agent.runs.contracts as run_contracts
import app.agent.runs.repository as run_repository
import app.queue.tasks.agent_run as agent_run_tasks
from app.agent.runs.daily_quota.contracts import DailyQuotaReleaseOutcome
from app.agent.runs.repository import AgentRunRepository
from app.models.agent_message import AgentMessage
from app.models.agent_run import AgentRun
from app.models.agent_thread import AgentThread
from app.models.agent_user_daily_quota import AgentUserDailyQuota
from app.queue.messages.agent_run import AgentRunTrigger
from tests.conftest import TEST_USER_ID
from tests.logfire._metric_helpers import collected_metrics

_USER_ID = uuid.UUID(TEST_USER_ID)
_USAGE_DATE = date(2026, 7, 22)
_DB_NOW = datetime(2026, 7, 22, 12, 0, tzinfo=UTC)
_MISSING = object()
_SENSITIVE_ACQUISITION_MARKERS = (
    str(_USER_ID),
    "SECRET_SQL_MARKER",
    "SECRET_QUESTION_MARKER",
    "SECRET_ANSWER_MARKER",
    "SECRET_PROVIDER_RAW_MARKER",
    "parameters:",
)
_SENSITIVE_ACQUISITION_ERROR = (
    "asyncpg failure SECRET_SQL_MARKER: UPDATE agent_user_daily_quotas "
    f"parameters: ('{_USER_ID}', 'SECRET_QUESTION_MARKER', "
    "'SECRET_ANSWER_MARKER', 'SECRET_PROVIDER_RAW_MARKER')"
)


class _SensitiveQuotaQueryFailure(RuntimeError):
    def __init__(self) -> None:
        super().__init__(_SENSITIVE_ACQUISITION_ERROR)
        self.params = {
            "user_id": str(_USER_ID),
            "question": "SECRET_QUESTION_MARKER",
            "answer": "SECRET_ANSWER_MARKER",
            "provider_raw": "SECRET_PROVIDER_RAW_MARKER",
        }


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
            started_at=_DB_NOW if status == "running" else None,
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


def _acquire_outcome(name: str) -> object:
    enum = getattr(run_contracts, "AcquireForExecutionOutcome", None)
    assert enum is not None, "AcquireForExecutionOutcome is not implemented"
    return getattr(enum, name)


def _queued_start_deadline_seconds() -> int:
    value = getattr(
        run_repository,
        "RESEARCH_QUEUED_START_DEADLINE_SECONDS",
        None,
    )
    assert value == 180
    return value


def _assert_acquire_result(
    result: object,
    *,
    outcome: str,
    prepared: bool,
    quota_release_outcome: DailyQuotaReleaseOutcome | None,
) -> None:
    command_outcome = getattr(run_contracts, "AcquireForExecutionCommandOutcome", None)
    assert command_outcome is not None, (
        "AcquireForExecutionCommandOutcome is not implemented"
    )
    assert isinstance(result, command_outcome)
    assert getattr(result, "acquire_outcome", _MISSING) is _acquire_outcome(outcome)
    prepared_run = getattr(result, "prepared_run", _MISSING)
    assert (prepared_run is not None) is prepared
    assert getattr(result, "quota_release_outcome", _MISSING) is quota_release_outcome


def _ctx(session_factory: async_sessionmaker[AsyncSession]) -> SimpleNamespace:
    return SimpleNamespace(state=SimpleNamespace(session_factory=session_factory))


def _assert_safe_acquisition_error(
    error: BaseException,
    logs: object,
) -> None:
    rendered_traceback = "".join(
        traceback.format_exception(type(error), error, error.__traceback__)
    )
    exposed_surfaces = (
        str(error),
        rendered_traceback,
        repr(error.__cause__),
        repr(error.__context__),
        repr(vars(error)),
        repr(logs),
    )

    assert error.__class__ is agent_run_tasks.AgentRunTaskBoundaryError
    assert error.args == ("agent run acquisition failed",)
    assert error.__cause__ is None
    assert error.__context__ is None
    assert error.__suppress_context__ is True
    assert not hasattr(error, "params")
    assert all(
        marker not in surface
        for marker in _SENSITIVE_ACQUISITION_MARKERS
        for surface in exposed_surfaces
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


def test_acquire_command_result_distinguishes_execution_expiry_and_skip() -> None:
    assert _queued_start_deadline_seconds() == 180
    command_outcome = getattr(run_contracts, "AcquireForExecutionCommandOutcome", None)
    assert command_outcome is not None, (
        "AcquireForExecutionCommandOutcome is not implemented"
    )
    assert set(inspect.signature(command_outcome).parameters) == {
        "acquire_outcome",
        "prepared_run",
        "quota_release_outcome",
    }
    acquire_outcome = getattr(run_contracts, "AcquireForExecutionOutcome")
    assert {member.value for member in acquire_outcome} == {
        "acquired",
        "queued_start_deadline_expired",
        "idempotent_skip",
    }


@pytest.mark.integration
@pytest.mark.asyncio
async def test_expired_queued_run_terminalizes_and_releases_original_quota_atomically(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    deadline_seconds = _queued_start_deadline_seconds()
    seeded = await _seed_queued_run(
        session_factory,
        created_at=_DB_NOW
        - timedelta(
            seconds=deadline_seconds,
            microseconds=1,
        ),
    )

    async with session_factory() as session:
        async with session.begin():
            result = await AgentRunRepository(session).acquire_for_execution(
                seeded.run_id,
                now=_DB_NOW,
            )

    _assert_acquire_result(
        result,
        outcome="QUEUED_START_DEADLINE_EXPIRED",
        prepared=False,
        quota_release_outcome=DailyQuotaReleaseOutcome.RELEASED,
    )
    run = await _read_run(session_factory, seeded.run_id)
    assert (run.status, run.error_code, run.attempt_epoch) == ("failed", "stale", 0)
    assert await _read_counter(session_factory, usage_date=_USAGE_DATE) == 0


@pytest.mark.integration
@pytest.mark.asyncio
async def test_queued_start_deadline_keeps_exact_boundary_and_expires_immediately_after(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    deadline_seconds = _queued_start_deadline_seconds()
    exact = await _seed_queued_run(
        session_factory,
        created_at=_DB_NOW - timedelta(seconds=deadline_seconds),
    )
    just_after = await _seed_queued_run(
        session_factory,
        created_at=_DB_NOW
        - timedelta(
            seconds=deadline_seconds,
            microseconds=1,
        ),
        quota_usage_date=date(2026, 7, 21),
    )

    async with session_factory() as session:
        async with session.begin():
            exact_result = await AgentRunRepository(session).acquire_for_execution(
                exact.run_id,
                now=_DB_NOW,
            )
            expired_result = await AgentRunRepository(session).acquire_for_execution(
                just_after.run_id,
                now=_DB_NOW,
            )

    _assert_acquire_result(
        exact_result,
        outcome="ACQUIRED",
        prepared=True,
        quota_release_outcome=None,
    )
    _assert_acquire_result(
        expired_result,
        outcome="QUEUED_START_DEADLINE_EXPIRED",
        prepared=False,
        quota_release_outcome=DailyQuotaReleaseOutcome.RELEASED,
    )
    exact_run = await _read_run(session_factory, exact.run_id)
    expired_run = await _read_run(session_factory, just_after.run_id)
    assert (exact_run.status, expired_run.status) == ("running", "failed")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_expired_legacy_queued_run_terminalizes_without_quota_release(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    seeded = await _seed_queued_run(
        session_factory,
        created_at=_DB_NOW - timedelta(seconds=181),
        quota_usage_date=None,
        counter_count=None,
    )

    async with session_factory() as session:
        async with session.begin():
            result = await AgentRunRepository(session).acquire_for_execution(
                seeded.run_id,
                now=_DB_NOW,
            )

    _assert_acquire_result(
        result,
        outcome="QUEUED_START_DEADLINE_EXPIRED",
        prepared=False,
        quota_release_outcome=DailyQuotaReleaseOutcome.NOT_ELIGIBLE,
    )
    run = await _read_run(session_factory, seeded.run_id)
    assert (run.status, run.error_code) == ("failed", "stale")


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("counter_count", [None, 0])
async def test_expired_queued_run_with_missing_or_empty_counter_is_inconsistent(
    session_factory: async_sessionmaker[AsyncSession],
    counter_count: int | None,
) -> None:
    seeded = await _seed_queued_run(
        session_factory,
        created_at=_DB_NOW - timedelta(seconds=181),
        counter_count=counter_count,
    )

    async with session_factory() as session:
        async with session.begin():
            result = await AgentRunRepository(session).acquire_for_execution(
                seeded.run_id,
                now=_DB_NOW,
            )

    _assert_acquire_result(
        result,
        outcome="QUEUED_START_DEADLINE_EXPIRED",
        prepared=False,
        quota_release_outcome=DailyQuotaReleaseOutcome.INCONSISTENT,
    )
    run = await _read_run(session_factory, seeded.run_id)
    assert run.status == "failed"
    assert await _read_counter(session_factory, usage_date=_USAGE_DATE) == counter_count


@pytest.mark.integration
@pytest.mark.asyncio
async def test_quota_query_failure_rolls_back_queued_expiry_without_a_committed_outcome(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    seeded = await _seed_queued_run(
        session_factory,
        created_at=_DB_NOW - timedelta(seconds=181),
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
    try:
        with pytest.raises(RuntimeError, match="queued expiry quota query failed"):
            async with session.begin():
                await AgentRunRepository(session).acquire_for_execution(
                    seeded.run_id,
                    now=_DB_NOW,
                )
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
async def test_cancel_winner_refunds_once_and_expired_acquire_reports_idempotent_skip(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    seeded = await _seed_queued_run(
        session_factory,
        created_at=_DB_NOW - timedelta(seconds=181),
    )

    async with (
        session_factory() as cancel_session,
        session_factory() as acquire_session,
        session_factory() as observer,
    ):
        acquire_task: asyncio.Task[object] | None = None
        try:
            await cancel_session.begin()
            cancelled = await AgentRunRepository(cancel_session).cancel_run_for_user(
                run_id=seeded.run_id,
                user_id=seeded.user_id,
                now=_DB_NOW,
            )

            await acquire_session.begin()
            acquire_pid = await acquire_session.scalar(text("SELECT pg_backend_pid()"))
            assert isinstance(acquire_pid, int)
            acquire_task = asyncio.create_task(
                AgentRunRepository(acquire_session).acquire_for_execution(
                    seeded.run_id,
                    now=_DB_NOW,
                )
            )
            await _wait_until_blocked(observer, acquire_pid)

            await cancel_session.commit()
            result = await asyncio.wait_for(acquire_task, timeout=5)
            await acquire_session.commit()
        finally:
            if acquire_task is not None:
                if not acquire_task.done():
                    acquire_task.cancel()
                await asyncio.gather(acquire_task, return_exceptions=True)
            for session in (cancel_session, acquire_session, observer):
                if session.in_transaction():
                    await session.rollback()

    assert (
        getattr(cancelled, "quota_release_outcome", None)
        is DailyQuotaReleaseOutcome.RELEASED
    )
    _assert_acquire_result(
        result,
        outcome="IDEMPOTENT_SKIP",
        prepared=False,
        quota_release_outcome=None,
    )
    run = await _read_run(session_factory, seeded.run_id)
    assert (run.status, run.error_code) == ("failed", "cancelled")
    assert await _read_counter(session_factory, usage_date=_USAGE_DATE) == 0


@pytest.mark.integration
@pytest.mark.asyncio
async def test_timely_acquire_and_running_redelivery_never_release_quota(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    seeded = await _seed_queued_run(
        session_factory,
        created_at=_DB_NOW - timedelta(seconds=180),
    )

    async with session_factory() as session:
        async with session.begin():
            acquired = await AgentRunRepository(session).acquire_for_execution(
                seeded.run_id,
                now=_DB_NOW,
            )
            redelivered = await AgentRunRepository(session).acquire_for_execution(
                seeded.run_id,
                now=_DB_NOW + timedelta(seconds=1),
            )

    _assert_acquire_result(
        acquired,
        outcome="ACQUIRED",
        prepared=True,
        quota_release_outcome=None,
    )
    _assert_acquire_result(
        redelivered,
        outcome="ACQUIRED",
        prepared=True,
        quota_release_outcome=None,
    )
    run = await _read_run(session_factory, seeded.run_id)
    assert (run.status, run.attempt_epoch, run.error_code) == ("running", 2, None)
    assert await _read_counter(session_factory, usage_date=_USAGE_DATE) == 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_expired_queued_task_skips_live_provider_and_emits_post_commit_telemetry(
    session_factory: async_sessionmaker[AsyncSession],
    capfire: CaptureLogfire,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seeded = await _seed_queued_run(
        session_factory,
        created_at=datetime.now(UTC) - timedelta(seconds=181),
    )
    forbidden_calls: list[str] = []

    def forbidden(name: str) -> object:
        def raise_if_called(*_args: object, **_kwargs: object) -> None:
            forbidden_calls.append(name)
            raise AssertionError(f"expired queued run must not create {name}")

        return raise_if_called

    monkeypatch.setattr(agent_run_tasks, "get_redis", forbidden("redis"))
    monkeypatch.setattr(
        agent_run_tasks,
        "AgentRunLiveEventPublisher",
        forbidden("live event publisher"),
    )
    monkeypatch.setattr(
        agent_run_tasks,
        "AgentRunLiveStreamPublisher",
        forbidden("live stream attempt"),
    )
    monkeypatch.setattr(
        agent_run_tasks,
        "AgentRunLiveAnswerDeltaReporter",
        forbidden("answer delta reporter"),
    )
    monkeypatch.setattr(
        agent_run_tasks,
        "AgentRunExecutionProbe",
        forbidden("execution probe"),
    )
    monkeypatch.setattr(
        agent_run_tasks,
        "build_answering_runner",
        forbidden("answering runner"),
    )

    with capture_logs() as logs:
        await agent_run_tasks.run_agent_answer(
            trigger=AgentRunTrigger(run_id=seeded.run_id),
            ctx=_ctx(session_factory),
        )

    assert forbidden_calls == []
    run = await _read_run(session_factory, seeded.run_id)
    assert (run.status, run.error_code, run.attempt_epoch) == ("failed", "stale", 0)
    assert await _read_counter(session_factory, usage_date=_USAGE_DATE) == 0
    assert [
        entry
        for entry in logs
        if entry.get("event") == "agent_run_queued_start_deadline_expired"
    ] == [
        {
            "quota_release_result": "released",
            "event": "agent_run_queued_start_deadline_expired",
            "log_level": "info",
        }
    ]
    assert {
        (point["value"], frozenset(point.get("attributes", {}).items()))
        for point in _quota_release_metric_points(capfire)
    } == {(1, frozenset({("result", "released")}))}


@pytest.mark.integration
@pytest.mark.asyncio
async def test_queued_expiry_rollback_emits_neither_log_nor_quota_metric(
    session_factory: async_sessionmaker[AsyncSession],
    capfire: CaptureLogfire,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seeded = await _seed_queued_run(
        session_factory,
        created_at=datetime.now(UTC) - timedelta(seconds=181),
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

    monkeypatch.setattr(
        agent_run_tasks,
        "get_redis",
        lambda: pytest.fail("rollback path must not start live dependencies"),
    )
    try:
        with (
            capture_logs() as logs,
            pytest.raises(agent_run_tasks.AgentRunTaskBoundaryError) as exc_info,
        ):
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

    _assert_safe_acquisition_error(exc_info.value, logs)
    assert not [
        entry
        for entry in logs
        if entry.get("event") == "agent_run_queued_start_deadline_expired"
    ]
    assert _quota_release_metric_points(capfire) == []
    run = await _read_run(session_factory, seeded.run_id)
    assert (run.status, run.error_code, run.attempt_epoch) == ("queued", None, 0)
    assert await _read_counter(session_factory, usage_date=_USAGE_DATE) == 1
