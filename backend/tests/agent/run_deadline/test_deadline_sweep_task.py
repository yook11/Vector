"""期限切れrunを確定するsweep taskの観測契約。"""

from __future__ import annotations

import traceback
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID

import pytest
from logfire.testing import CaptureLogfire
from sqlalchemy import event as sa_event
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from structlog.testing import capture_logs

import app.queue.tasks.agent_run as agent_run_tasks
from app.agent.daily_quota import observability as daily_quota_observability
from app.agent.live_updates.stream import AgentRunLiveStreamTerminalEvent
from app.models.agent_run import AgentRun
from app.models.agent_user_daily_quota import AgentUserDailyQuota
from app.queue.tasks.agent_run import AgentRunTaskBoundaryError
from tests.agent.runs._seed import (
    create_thread_message_run as _create_thread_message_run,
)
from tests.conftest import TEST_USER_ID
from tests.logfire._metric_helpers import collected_metrics

SENSITIVE_TASK_BOUNDARY_MARKERS = (
    TEST_USER_ID,
    "SECRET_SQL_MARKER",
    "SECRET_QUESTION_MARKER",
    "SECRET_ANSWER_MARKER",
    "SECRET_PROVIDER_RAW_MARKER",
    "parameters:",
)
SENSITIVE_TASK_BOUNDARY_ERROR = (
    "asyncpg failure SECRET_SQL_MARKER: UPDATE agent_runs "
    f"parameters: ('{TEST_USER_ID}', 'SECRET_QUESTION_MARKER', "
    "'SECRET_ANSWER_MARKER', 'SECRET_PROVIDER_RAW_MARKER')"
)


class SensitivePersistenceFailure(RuntimeError):
    def __init__(self) -> None:
        super().__init__(SENSITIVE_TASK_BOUNDARY_ERROR)
        self.params = {
            "user_id": TEST_USER_ID,
            "question": "SECRET_QUESTION_MARKER",
            "answer": "SECRET_ANSWER_MARKER",
            "provider_raw": "SECRET_PROVIDER_RAW_MARKER",
        }


def _assert_safe_task_boundary_error(
    error: BaseException,
    *,
    expected_message: str,
) -> None:
    rendered_traceback = "".join(
        traceback.format_exception(type(error), error, error.__traceback__)
    )

    assert error.__class__ is AgentRunTaskBoundaryError
    assert str(error) == expected_message
    assert error.args == (expected_message,)
    assert error.__cause__ is None
    assert error.__context__ is None
    assert error.__suppress_context__ is True
    assert not hasattr(error, "params")
    assert all(marker not in str(error) for marker in SENSITIVE_TASK_BOUNDARY_MARKERS)
    assert all(
        marker not in repr(vars(error)) for marker in SENSITIVE_TASK_BOUNDARY_MARKERS
    )
    assert all(
        marker not in rendered_traceback for marker in SENSITIVE_TASK_BOUNDARY_MARKERS
    )
    assert all(
        marker not in repr(error.__cause__)
        for marker in SENSITIVE_TASK_BOUNDARY_MARKERS
    )


def _assert_sensitive_task_context_not_logged(logs: object) -> None:
    serialized_logs = repr(logs)
    assert all(
        marker not in serialized_logs for marker in SENSITIVE_TASK_BOUNDARY_MARKERS
    )


class FakeLiveStreamPublisher:
    instances: list[FakeLiveStreamPublisher] = []
    raise_on_begin = False
    raise_on_publish = False
    publish_outcomes: list[str | None | BaseException] = []

    def __init__(self, redis: object, run_id: UUID, attempt_epoch: int) -> None:
        self.redis = redis
        self.run_id = run_id
        self.attempt_epoch = attempt_epoch
        self.begin_attempt_calls = 0
        self.published: list[object] = []
        FakeLiveStreamPublisher.instances.append(self)

    async def begin_attempt(self) -> str | None:
        self.begin_attempt_calls += 1
        if self.raise_on_begin:
            raise RuntimeError("Redis unavailable")
        return "attempt-0"

    async def publish(self, event: object) -> str | None:
        self.published.append(event)
        if self.raise_on_publish:
            raise RuntimeError("Redis unavailable")
        if self.publish_outcomes:
            outcome = self.publish_outcomes.pop(0)
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome
        return f"{len(self.published)}-0"


def _ctx(session_factory: async_sessionmaker[AsyncSession]) -> SimpleNamespace:
    return SimpleNamespace(
        state=SimpleNamespace(
            session_factory=session_factory,
            agent_live_redis=object(),
        )
    )


def _quota_stale_metric_points(capfire: CaptureLogfire) -> list[dict[str, Any]]:
    metric = next(
        (
            item
            for item in collected_metrics(capfire)
            if item["name"] == "agent_user_daily_quota_stale_reservations_total"
        ),
        None,
    )
    if metric is None:
        return []
    return list(metric["data"]["data_points"])


@pytest.mark.integration
@pytest.mark.asyncio
async def test_sweep_task_observes_queued_release_and_running_reservation_after_commit(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deadline_at = datetime.now(UTC) - timedelta(seconds=1)
    usage_date = date(2026, 7, 20)
    async with session_factory() as session:
        session.add(
            AgentUserDailyQuota(
                user_id=UUID(TEST_USER_ID),
                usage_date=usage_date,
                used_count=1,
            )
        )
        _t1, _m1, old_queued = await _create_thread_message_run(
            session,
            question="sensitive queued question",
            created_at=deadline_at - timedelta(seconds=60),
            deadline_at=deadline_at,
            quota_usage_date=usage_date,
        )
        _t2, _m2, old_running = await _create_thread_message_run(
            session,
            question="sensitive running question",
            status="running",
            created_at=deadline_at - timedelta(seconds=60),
            deadline_at=deadline_at,
            attempt_epoch=2,
            quota_usage_date=usage_date,
        )
        _t3, _m3, old_legacy = await _create_thread_message_run(
            session,
            question="sensitive legacy question",
            created_at=deadline_at - timedelta(seconds=60),
            deadline_at=deadline_at,
        )

    release_metrics: list[dict[str, object]] = []
    reservations: list[dict[str, int]] = []

    def record_release(**kwargs: object) -> None:
        release_metrics.append(kwargs)

    def observe_stale_reservations(*, queued_count: int, running_count: int) -> None:
        reservations.append(
            {"queued_count": queued_count, "running_count": running_count}
        )

    monkeypatch.setattr(
        daily_quota_observability,
        "record_daily_quota_release",
        record_release,
    )
    monkeypatch.setattr(
        daily_quota_observability,
        "observe_stale_reservations",
        observe_stale_reservations,
    )
    monkeypatch.setattr(
        agent_run_tasks,
        "AgentRunLiveStreamPublisher",
        FakeLiveStreamPublisher,
    )

    with capture_logs() as logs:
        await agent_run_tasks.sweep_deadline_exceeded_agent_runs(
            ctx=_ctx(session_factory)
        )

    assert [
        entry
        for entry in logs
        if entry.get("event") == "agent_runs_queued_deadline_swept"
    ] == [
        {
            "event": "agent_runs_queued_deadline_swept",
            "log_level": "info",
            "run_count": 2,
            "quota_released_count": 1,
            "quota_not_eligible_count": 1,
            "quota_inconsistent_count": 0,
        }
    ]
    assert release_metrics == [
        {"result": "released", "count": 1},
        {"result": "not_eligible", "count": 1},
    ]
    assert reservations == [{"queued_count": 0, "running_count": 1}]
    assert "sensitive" not in str(logs)

    async with session_factory() as session:
        counter = await session.scalar(
            select(AgentUserDailyQuota.used_count).where(
                AgentUserDailyQuota.user_id == UUID(TEST_USER_ID),
                AgentUserDailyQuota.usage_date == usage_date,
            )
        )
        statuses = [
            await session.get(AgentRun, run_id)
            for run_id in (old_queued.id, old_running.id, old_legacy.id)
        ]
    assert counter == 0
    assert [run.status if run is not None else None for run in statuses] == [
        "deadline_exceeded",
        "deadline_exceeded",
        "deadline_exceeded",
    ]


@pytest.mark.asyncio
async def test_sweep_task_legacy_deadline_batch_emits_no_quota_alert_or_metric(
    session_factory: async_sessionmaker[AsyncSession],
    capfire: CaptureLogfire,
) -> None:
    deadline_at = datetime.now(UTC) - timedelta(seconds=1)
    async with session_factory() as session:
        _thread, _message, legacy = await _create_thread_message_run(
            session,
            question="sensitive legacy-only question",
            created_at=deadline_at - timedelta(seconds=60),
            deadline_at=deadline_at,
        )

    with capture_logs() as logs:
        await agent_run_tasks.sweep_deadline_exceeded_agent_runs(
            ctx=_ctx(session_factory)
        )

    assert [
        entry for entry in logs if entry.get("event") == "agent_runs_deadline_swept"
    ] == [{"count": 1, "event": "agent_runs_deadline_swept", "log_level": "info"}]
    assert not [
        entry
        for entry in logs
        if entry.get("event") == "agent_user_daily_quota_stale_reservations_retained"
    ]
    assert _quota_stale_metric_points(capfire) == []

    async with session_factory() as session:
        swept = await session.get(AgentRun, legacy.id)
    assert swept is not None and swept.status == "deadline_exceeded"


@pytest.mark.asyncio
async def test_empty_sweep_emits_total_only_without_quota_alert_or_metric(
    session_factory: async_sessionmaker[AsyncSession],
    capfire: CaptureLogfire,
) -> None:
    with capture_logs() as logs:
        await agent_run_tasks.sweep_deadline_exceeded_agent_runs(
            ctx=_ctx(session_factory)
        )

    assert [
        entry for entry in logs if entry.get("event") == "agent_runs_deadline_swept"
    ] == [{"count": 0, "event": "agent_runs_deadline_swept", "log_level": "info"}]
    assert not [
        entry
        for entry in logs
        if entry.get("event") == "agent_user_daily_quota_stale_reservations_retained"
    ]
    assert _quota_stale_metric_points(capfire) == []


@pytest.mark.asyncio
async def test_sweep_task_does_not_observe_quota_results_when_transaction_rolls_back(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deadline_at = datetime.now(UTC) - timedelta(seconds=1)
    async with session_factory() as setup_session:
        _thread, _message, expired_run = await _create_thread_message_run(
            setup_session,
            created_at=deadline_at - timedelta(seconds=60),
            deadline_at=deadline_at,
            quota_usage_date=date(2026, 7, 20),
        )

    calls: list[dict[str, int]] = []
    commit_attempted = False

    def observe_stale_reservations(*, queued_count: int, running_count: int) -> None:
        calls.append(
            {
                "queued_count": queued_count,
                "running_count": running_count,
            }
        )

    def fail_commit(_session: object) -> None:
        nonlocal commit_attempted
        commit_attempted = True
        raise SensitivePersistenceFailure

    monkeypatch.setattr(
        daily_quota_observability,
        "observe_stale_reservations",
        observe_stale_reservations,
    )
    failing_session = session_factory()
    sa_event.listen(
        failing_session.sync_session,
        "before_commit",
        fail_commit,
        once=True,
    )

    def failing_session_factory() -> AsyncSession:
        return failing_session

    with (
        capture_logs() as logs,
        pytest.raises(Exception) as exc_info,
    ):
        await agent_run_tasks.sweep_deadline_exceeded_agent_runs(
            ctx=_ctx(
                cast(
                    async_sessionmaker[AsyncSession],
                    failing_session_factory,
                )
            )
        )

    _assert_safe_task_boundary_error(
        exc_info.value,
        expected_message="agent run deadline sweep failed",
    )
    _assert_sensitive_task_context_not_logged(logs)
    assert commit_attempted is True
    assert calls == []
    assert not [
        entry
        for entry in logs
        if entry.get("event")
        in {
            "agent_runs_deadline_swept",
            "agent_user_daily_quota_stale_reservations_retained",
        }
    ]

    async with session_factory() as verification:
        persisted = await verification.get(AgentRun, expired_run.id)
    assert persisted is not None
    assert persisted.status == "queued"
    assert persisted.error_code is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failing_sink",
    ["total_log", "quota_log", "queued_metric", "running_metric"],
)
async def test_sweep_task_telemetry_sink_failure_keeps_committed_sweep_and_other_sinks(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    failing_sink: str,
) -> None:
    deadline_at = datetime.now(UTC) - timedelta(seconds=1)
    usage_date = date(2026, 7, 20)
    async with session_factory() as setup_session:
        _queued_thread, _queued_message, queued = await _create_thread_message_run(
            setup_session,
            question="queued deadline telemetry isolation",
            created_at=deadline_at - timedelta(seconds=60),
            deadline_at=deadline_at,
            quota_usage_date=usage_date,
        )
        _running_thread, _running_message, running = await _create_thread_message_run(
            setup_session,
            question="running deadline telemetry isolation",
            status="running",
            created_at=deadline_at - timedelta(seconds=60),
            deadline_at=deadline_at,
            quota_usage_date=usage_date,
        )
    attempts: list[str] = []

    def record_total_log(event: str, **_kwargs: object) -> None:
        if event == "agent_runs_deadline_swept":
            attempts.append("total_log")
            if failing_sink == "total_log":
                raise RuntimeError("total deadline log sink unavailable")

    def record_quota_log(event: str, **_kwargs: object) -> None:
        if event == "agent_user_daily_quota_stale_reservations_retained":
            attempts.append("quota_log")
            if failing_sink == "quota_log":
                raise RuntimeError("quota stale log sink unavailable")

    def record_stale_reservation(*, previous_status: str, count: int = 1) -> None:
        assert count == 1
        attempts.append(f"{previous_status}_metric")
        if failing_sink == f"{previous_status}_metric":
            raise RuntimeError("quota stale metric sink unavailable")

    monkeypatch.setattr(agent_run_tasks.logger, "info", record_total_log)
    monkeypatch.setattr(daily_quota_observability.logger, "warning", record_quota_log)
    monkeypatch.setattr(
        daily_quota_observability,
        "record_daily_quota_stale_reservation",
        record_stale_reservation,
    )

    await agent_run_tasks.sweep_deadline_exceeded_agent_runs(ctx=_ctx(session_factory))

    assert set(attempts) == {
        "total_log",
        "quota_log",
        "queued_metric",
        "running_metric",
    }
    async with session_factory() as verification:
        persisted = [
            await verification.get(AgentRun, run_id)
            for run_id in (queued.id, running.id)
        ]
    assert [run.status if run is not None else None for run in persisted] == [
        "deadline_exceeded",
        "deadline_exceeded",
    ]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_sweep_task_batches_queued_quota_observability_after_commit(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    usage_date = date(2026, 7, 22)
    missing_counter_date = date(2026, 7, 21)
    deadline_at = datetime.now(UTC) - timedelta(seconds=1)
    async with session_factory() as session:
        session.add(
            AgentUserDailyQuota(
                user_id=UUID(TEST_USER_ID),
                usage_date=usage_date,
                used_count=2,
            )
        )
        released_runs = [
            (
                await _create_thread_message_run(
                    session,
                    question=f"sensitive released queued {index}",
                    created_at=deadline_at - timedelta(seconds=60),
                    deadline_at=deadline_at,
                    quota_usage_date=usage_date,
                )
            )[2]
            for index in range(2)
        ]
        legacy = (
            await _create_thread_message_run(
                session,
                question="sensitive legacy queued",
                created_at=deadline_at - timedelta(seconds=60),
                deadline_at=deadline_at,
            )
        )[2]
        inconsistent = (
            await _create_thread_message_run(
                session,
                question="sensitive inconsistent queued",
                created_at=deadline_at - timedelta(seconds=60),
                deadline_at=deadline_at,
                quota_usage_date=missing_counter_date,
            )
        )[2]

    metric_calls: list[dict[str, object]] = []

    def record_release(**kwargs: object) -> None:
        metric_calls.append(kwargs)

    monkeypatch.setattr(
        daily_quota_observability,
        "record_daily_quota_release",
        record_release,
    )
    FakeLiveStreamPublisher.instances = []
    monkeypatch.setattr(
        agent_run_tasks,
        "AgentRunLiveStreamPublisher",
        FakeLiveStreamPublisher,
    )

    with capture_logs() as logs:
        await agent_run_tasks.sweep_deadline_exceeded_agent_runs(
            ctx=_ctx(session_factory)
        )

    batch_logs = [
        entry
        for entry in logs
        if entry.get("event") == "agent_runs_queued_deadline_swept"
    ]
    assert batch_logs == [
        {
            "event": "agent_runs_queued_deadline_swept",
            "log_level": "info",
            "run_count": 4,
            "quota_released_count": 2,
            "quota_not_eligible_count": 1,
            "quota_inconsistent_count": 1,
        }
    ]
    assert metric_calls == [
        {"result": "released", "count": 2},
        {"result": "not_eligible", "count": 1},
        {"result": "inconsistent", "count": 1},
    ]
    assert FakeLiveStreamPublisher.instances == []
    assert "sensitive" not in str(logs)
    async with session_factory() as session:
        counter = await session.scalar(
            select(AgentUserDailyQuota.used_count).where(
                AgentUserDailyQuota.user_id == UUID(TEST_USER_ID),
                AgentUserDailyQuota.usage_date == usage_date,
            )
        )
        statuses = [
            await session.get(AgentRun, run_id)
            for run_id in (
                *(run.id for run in released_runs),
                legacy.id,
                inconsistent.id,
            )
        ]
    assert counter == 0
    for persisted in statuses:
        assert persisted is not None
        assert persisted.status == "deadline_exceeded"
        assert persisted.error_code is None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_sweep_task_publishes_each_committed_running_attempt_despite_failure(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deadline_at = datetime.now(UTC) - timedelta(seconds=1)
    async with session_factory() as session:
        normal = (
            await _create_thread_message_run(
                session,
                status="running",
                question="sensitive normal running",
                created_at=deadline_at - timedelta(seconds=60),
                deadline_at=deadline_at,
                attempt_epoch=2,
            )
        )[2]
        another_running = (
            await _create_thread_message_run(
                session,
                status="running",
                question="sensitive another running",
                created_at=deadline_at - timedelta(seconds=60),
                deadline_at=deadline_at,
                attempt_epoch=3,
            )
        )[2]

    class CommitCheckingPublisher(FakeLiveStreamPublisher):
        async def publish(self, event: object) -> str | None:
            async with session_factory() as verification:
                persisted = await verification.get(AgentRun, self.run_id)
            assert persisted is not None
            assert persisted.status == "deadline_exceeded"
            assert persisted.error_code is None
            return await super().publish(event)

    FakeLiveStreamPublisher.instances = []
    CommitCheckingPublisher.publish_outcomes = [RuntimeError("redis unavailable"), None]
    monkeypatch.setattr(
        agent_run_tasks,
        "AgentRunLiveStreamPublisher",
        CommitCheckingPublisher,
    )

    with capture_logs() as logs:
        await agent_run_tasks.sweep_deadline_exceeded_agent_runs(
            ctx=_ctx(session_factory)
        )

    assert {
        (publisher.run_id, publisher.attempt_epoch)
        for publisher in FakeLiveStreamPublisher.instances
    } == {(normal.id, 2), (another_running.id, 3)}
    assert [
        event
        for publisher in FakeLiveStreamPublisher.instances
        for event in publisher.published
    ] == [
        AgentRunLiveStreamTerminalEvent(status="deadline_exceeded"),
        AgentRunLiveStreamTerminalEvent(status="deadline_exceeded"),
    ]
    assert [
        entry
        for entry in logs
        if entry.get("event") == "running_deadline_exceeded_swept"
    ] == [{"count": 2, "event": "running_deadline_exceeded_swept", "log_level": "info"}]
    assert (
        len(
            [
                entry
                for entry in logs
                if entry.get("event") == "agent_run_live_stream_terminal_publish_failed"
            ]
        )
        == 1
    )
    assert "sensitive" not in str(logs)
    for original in (normal, another_running):
        persisted = await _persisted_run_for_sweep_test(session_factory, original.id)
        assert persisted.status == "deadline_exceeded"
        assert persisted.error_code is None


async def _persisted_run_for_sweep_test(
    session_factory: async_sessionmaker[AsyncSession], run_id: UUID
) -> AgentRun:
    async with session_factory() as session:
        run = await session.get(AgentRun, run_id)
    assert run is not None
    return run


@pytest.mark.integration
@pytest.mark.asyncio
async def test_sweep_task_emits_no_queued_result_or_event_when_commit_rolls_back(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deadline_at = datetime.now(UTC) - timedelta(seconds=1)
    usage_date = date(2026, 7, 22)
    async with session_factory() as setup_session:
        _thread, _message, run = await _create_thread_message_run(
            setup_session,
            question="sensitive rollback queued",
            created_at=deadline_at - timedelta(seconds=60),
            deadline_at=deadline_at,
            quota_usage_date=usage_date,
        )
        setup_session.add(
            AgentUserDailyQuota(
                user_id=UUID(TEST_USER_ID),
                usage_date=usage_date,
                used_count=1,
            )
        )
        await setup_session.commit()

    metric_calls: list[dict[str, object]] = []
    failing_session = session_factory()

    def fail_commit(_session: object) -> None:
        raise SensitivePersistenceFailure

    def record_release(**kwargs: object) -> None:
        metric_calls.append(kwargs)

    sa_event.listen(
        failing_session.sync_session,
        "before_commit",
        fail_commit,
        once=True,
    )
    monkeypatch.setattr(
        daily_quota_observability,
        "record_daily_quota_release",
        record_release,
    )

    def failing_session_factory() -> AsyncSession:
        return failing_session

    try:
        with (
            capture_logs() as logs,
            pytest.raises(Exception) as exc_info,
        ):
            await agent_run_tasks.sweep_deadline_exceeded_agent_runs(
                ctx=_ctx(
                    cast(
                        async_sessionmaker[AsyncSession],
                        failing_session_factory,
                    )
                )
            )
    finally:
        await failing_session.close()

    _assert_safe_task_boundary_error(
        exc_info.value,
        expected_message="agent run deadline sweep failed",
    )
    _assert_sensitive_task_context_not_logged(logs)
    assert metric_calls == []
    assert not [
        entry
        for entry in logs
        if entry.get("event")
        in {
            "agent_runs_queued_deadline_swept",
            "running_deadline_exceeded_swept",
        }
    ]
    persisted = await _persisted_run_for_sweep_test(session_factory, run.id)
    assert persisted.status == "queued"
    assert persisted.error_code is None
