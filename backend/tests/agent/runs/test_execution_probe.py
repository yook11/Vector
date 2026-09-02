"""Agent run execution continuation probe の契約。"""

from __future__ import annotations

from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID

import pytest
from logfire.testing import CaptureLogfire
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from structlog.testing import capture_logs

from app.agent.runs.execution import Continue, Stop, StopReason
from app.agent.runs.execution_probe import AgentRunExecutionProbe
from app.agent.runs.repository import AgentRunRepository
from app.models.agent_message import AgentMessage
from app.models.agent_run import AgentRun
from app.models.agent_thread import AgentThread
from tests.agent.runs._start_run_outcomes import started_attempt_epoch
from tests.conftest import TEST_USER_ID
from tests.logfire._metric_helpers import collected_metrics

RUN_ID = UUID("00000000-0000-4000-a000-000000000011")
ATTEMPT_EPOCH = 3
UNAVAILABLE_METRIC = "vector.agent.execution_probe.unavailable"
_STOP_NOT_CURRENT = Stop(StopReason.NOT_CURRENT)


@dataclass
class ManualClock:
    now: float = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class _NullTransaction:
    async def __aenter__(self) -> _NullTransaction:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None


class FakeSession:
    def __init__(self) -> None:
        self.entered = 0
        self.exited = 0

    async def __aenter__(self) -> FakeSession:
        self.entered += 1
        return self

    async def __aexit__(self, *args: object) -> None:
        self.exited += 1

    def begin(self) -> _NullTransaction:
        return _NullTransaction()


class RaisingSessionContext:
    def __init__(self, exception: BaseException) -> None:
        self._exception = exception
        self.entered = 0

    async def __aenter__(self) -> None:
        self.entered += 1
        raise self._exception

    async def __aexit__(self, *args: object) -> None:
        return None


class FakeSessionFactory:
    def __init__(self, *, fail_on_enter: BaseException | None = None) -> None:
        self._fail_on_enter = fail_on_enter
        self.sessions: list[FakeSession | RaisingSessionContext] = []

    def __call__(self) -> FakeSession | RaisingSessionContext:
        if self._fail_on_enter is not None and not self.sessions:
            session: FakeSession | RaisingSessionContext = RaisingSessionContext(
                self._fail_on_enter
            )
        else:
            session = FakeSession()
        self.sessions.append(session)
        return session


def _new_probe(
    session_factory: object,
    clock: ManualClock,
    *,
    run_id: UUID = RUN_ID,
    attempt_epoch: int = ATTEMPT_EPOCH,
) -> AgentRunExecutionProbe:
    return AgentRunExecutionProbe(
        cast("async_sessionmaker[AsyncSession]", session_factory),
        run_id,
        attempt_epoch,
        clock=clock,
    )


def _patch_decisions(
    monkeypatch: pytest.MonkeyPatch,
    outcomes: Sequence[Continue | Stop | BaseException],
) -> None:
    pending = deque(outcomes)

    async def decide(
        self: AgentRunRepository,
        **_kwargs: object,
    ) -> Continue | Stop:
        item = pending.popleft()
        if isinstance(item, BaseException):
            raise item
        return item

    monkeypatch.setattr(AgentRunRepository, "decide_execution_continuation", decide)


def _metric_points(capfire: CaptureLogfire) -> list[dict[str, Any]]:
    metric = next(
        (
            item
            for item in collected_metrics(capfire)
            if item["name"] == UNAVAILABLE_METRIC
        ),
        None,
    )
    if metric is None:
        return []
    return list(metric["data"]["data_points"])


@pytest.mark.asyncio
async def test_probe_cache_rechecks_at_exactly_two_seconds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_decisions(monkeypatch, [Continue(), _STOP_NOT_CURRENT])
    factory = FakeSessionFactory()
    clock = ManualClock()
    probe = _new_probe(factory, clock)

    assert await probe.should_continue() == Continue()
    clock.advance(1.999)
    assert await probe.should_continue() == Continue()
    assert len(factory.sessions) == 1

    clock.now = 2.0
    assert await probe.should_continue() == _STOP_NOT_CURRENT
    assert len(factory.sessions) == 2
    assert all(session.entered == 1 for session in factory.sessions)
    assert all(
        isinstance(session, FakeSession) and session.exited == 1
        for session in factory.sessions
    )


@pytest.mark.asyncio
async def test_stop_result_is_terminal_cache_without_later_database_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_decisions(monkeypatch, [_STOP_NOT_CURRENT, Continue()])
    factory = FakeSessionFactory()
    clock = ManualClock()
    probe = _new_probe(factory, clock)

    assert await probe.should_continue() == _STOP_NOT_CURRENT
    clock.advance(200.0)
    assert await probe.should_continue() == _STOP_NOT_CURRENT
    assert len(factory.sessions) == 1


@pytest.mark.asyncio
async def test_database_check_failure_fails_open_and_recovers_after_cache_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_decisions(
        monkeypatch,
        [RuntimeError("DB_SECRET"), _STOP_NOT_CURRENT],
    )
    factory = FakeSessionFactory()
    clock = ManualClock()
    probe = _new_probe(factory, clock)

    assert await probe.should_continue() == Continue()
    clock.advance(1.999)
    assert await probe.should_continue() == Continue()
    assert len(factory.sessions) == 1

    clock.now = 2.0
    assert await probe.should_continue() == _STOP_NOT_CURRENT
    assert len(factory.sessions) == 2


@pytest.mark.asyncio
async def test_session_open_failure_also_fails_open_and_is_cached() -> None:
    factory = FakeSessionFactory(fail_on_enter=RuntimeError("SESSION_SECRET"))
    clock = ManualClock()
    probe = _new_probe(factory, clock)

    assert await probe.should_continue() == Continue()
    clock.advance(1.0)
    assert await probe.should_continue() == Continue()
    assert len(factory.sessions) == 1


@pytest.mark.asyncio
async def test_unavailable_observation_is_once_per_real_failure_and_pii_free(
    capfire: CaptureLogfire,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_decisions(
        monkeypatch,
        [RuntimeError("DB_EXCEPTION_SECRET"), RuntimeError("DB_EXCEPTION_SECRET")],
    )
    factory = FakeSessionFactory()
    clock = ManualClock()
    probe = _new_probe(factory, clock)
    answer = "ANSWER_SECRET"
    question = "QUESTION_SECRET"
    user_id = "USER_ID_SECRET"

    with capture_logs() as logs:
        assert await probe.should_continue() == Continue()
        clock.advance(1.0)
        assert await probe.should_continue() == Continue()
        clock.advance(1.0)
        assert await probe.should_continue() == Continue()

    unavailable_logs = [
        entry
        for entry in logs
        if entry.get("event") == "agent_run_execution_probe_unavailable"
    ]
    assert len(unavailable_logs) == 2
    assert all(entry["run_id"] == str(RUN_ID) for entry in unavailable_logs)
    assert all(entry["attempt_epoch"] == ATTEMPT_EPOCH for entry in unavailable_logs)
    serialized_logs = repr(logs)
    assert "DB_EXCEPTION_SECRET" not in serialized_logs
    assert answer not in serialized_logs
    assert question not in serialized_logs
    assert user_id not in serialized_logs

    points = _metric_points(capfire)
    assert sum(int(point["value"]) for point in points) == 2
    assert all(
        point.get("attributes", {}) == {"reason": "database_unavailable"}
        for point in points
    )


async def _create_run(
    session: AsyncSession,
    *,
    status: str,
    attempt_epoch: int,
    deadline_at: datetime | None = None,
) -> AgentRun:
    thread = AgentThread(
        user_id=UUID(TEST_USER_ID),
        title=f"probe {status}",
        updated_at=datetime(2026, 7, 12, tzinfo=UTC),
    )
    session.add(thread)
    await session.flush()
    user_message = AgentMessage(
        thread_id=thread.id,
        seq=1,
        role="user",
        content="probe question",
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
            content="probe answer",
            missing_aspects=[],
        )
        session.add(assistant_message)
        await session.flush()
        assistant_message_id = assistant_message.id
    created_at = datetime.now(UTC)
    run = AgentRun(
        thread_id=thread.id,
        user_message_id=user_message.id,
        assistant_message_id=assistant_message_id,
        status=status,
        error_code="internal_error" if status == "failed" else None,
        attempt_epoch=attempt_epoch,
        created_at=created_at,
        deadline_at=deadline_at or created_at + timedelta(seconds=60),
    )
    session.add(run)
    await session.commit()
    await session.refresh(run)
    return run


@pytest.mark.integration
@pytest.mark.asyncio
async def test_actual_cancel_commit_makes_cached_probe_stop_after_two_seconds(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as setup_session:
        run = await _create_run(
            setup_session,
            status="running",
            attempt_epoch=1,
        )
    clock = ManualClock()
    probe = _new_probe(
        session_factory,
        clock,
        run_id=run.id,
        attempt_epoch=1,
    )
    assert await probe.should_continue() == Continue()

    async with session_factory() as cancel_session:
        async with cancel_session.begin():
            result = await AgentRunRepository(cancel_session).cancel_run_for_user(
                run_id=run.id,
                user_id=UUID(TEST_USER_ID),
            )
    assert result is not None
    clock.advance(2.0)

    assert await probe.should_continue() == _STOP_NOT_CURRENT


@pytest.mark.integration
@pytest.mark.asyncio
async def test_actual_restart_makes_old_epoch_probe_stop_after_two_seconds(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as setup_session:
        run = await _create_run(
            setup_session,
            status="running",
            attempt_epoch=1,
        )
    clock = ManualClock()
    probe = _new_probe(
        session_factory,
        clock,
        run_id=run.id,
        attempt_epoch=1,
    )
    assert await probe.should_continue() == Continue()

    async with session_factory() as start_session:
        async with start_session.begin():
            attempt_epoch = started_attempt_epoch(
                await AgentRunRepository(start_session).start_run(run.id)
            )
    assert attempt_epoch == 2
    clock.advance(2.0)

    assert await probe.should_continue() == _STOP_NOT_CURRENT


@pytest.mark.integration
@pytest.mark.asyncio
async def test_expired_deadline_stops_through_choke_and_writes_once(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as setup_session:
        run = await _create_run(
            setup_session,
            status="running",
            attempt_epoch=1,
            deadline_at=datetime.now(UTC) - timedelta(seconds=1),
        )
    clock = ManualClock()
    probe = _new_probe(
        session_factory,
        clock,
        run_id=run.id,
        attempt_epoch=1,
    )
    assert await probe.should_continue() == Stop(StopReason.DEADLINE_EXCEEDED)
    clock.advance(200.0)
    assert await probe.should_continue() == Stop(StopReason.DEADLINE_EXCEEDED)

    async with session_factory() as session:
        persisted = await session.get(AgentRun, run.id)
    assert persisted is not None
    assert persisted.status == "deadline_exceeded"
    assert persisted.attempt_epoch == 1
    assert persisted.error_code is None
