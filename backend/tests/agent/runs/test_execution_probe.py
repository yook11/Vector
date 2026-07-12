"""Agent run execution continuation probe の契約。"""

from __future__ import annotations

from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib import import_module
from typing import Any, Protocol, cast
from uuid import UUID

import pytest
from logfire.testing import CaptureLogfire
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from structlog.testing import capture_logs

from app.agent.runs.repository import AgentRunRepository
from app.models.agent_message import AgentMessage
from app.models.agent_run import AgentRun
from app.models.agent_thread import AgentThread
from tests.conftest import TEST_USER_ID
from tests.logfire._metric_helpers import collected_metrics

RUN_ID = UUID("00000000-0000-4000-a000-000000000011")
MISSING_RUN_ID = UUID("00000000-0000-4000-a000-000000000099")
ATTEMPT_EPOCH = 3
UNAVAILABLE_METRIC = "vector.agent.execution_probe.unavailable"


class _ExecutionProbe(Protocol):
    async def should_continue(self) -> bool: ...


@dataclass
class ManualClock:
    now: float = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeScalarResult:
    def __init__(self, exists: bool) -> None:
        self._value = 1 if exists else None

    def scalar_one_or_none(self) -> int | None:
        return self._value


class FakeSession:
    def __init__(self, outcome: bool | BaseException) -> None:
        self._outcome = outcome
        self.entered = 0
        self.exited = 0
        self.commit_calls = 0
        self.statements: list[object] = []

    async def __aenter__(self) -> FakeSession:
        self.entered += 1
        return self

    async def __aexit__(self, *args: object) -> None:
        self.exited += 1

    async def execute(self, statement: object) -> FakeScalarResult:
        self.statements.append(statement)
        if isinstance(self._outcome, BaseException):
            raise self._outcome
        return FakeScalarResult(self._outcome)

    async def commit(self) -> None:
        self.commit_calls += 1


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
    def __init__(
        self,
        outcomes: Sequence[bool | BaseException],
        *,
        fail_on_enter: bool = False,
    ) -> None:
        self._outcomes = deque(outcomes)
        self._fail_on_enter = fail_on_enter
        self.sessions: list[FakeSession | RaisingSessionContext] = []

    def __call__(self) -> FakeSession | RaisingSessionContext:
        outcome = self._outcomes.popleft()
        if self._fail_on_enter:
            assert isinstance(outcome, BaseException)
            session: FakeSession | RaisingSessionContext = RaisingSessionContext(
                outcome
            )
        else:
            session = FakeSession(outcome)
        self.sessions.append(session)
        return session


def _new_probe(
    session_factory: object,
    clock: ManualClock,
    *,
    run_id: UUID = RUN_ID,
    attempt_epoch: int = ATTEMPT_EPOCH,
) -> _ExecutionProbe:
    try:
        module = import_module("app.agent.runs.execution_probe")
    except ModuleNotFoundError as exc:
        if exc.name != "app.agent.runs.execution_probe":
            raise
        pytest.fail("Agent run execution probe が未実装です", pytrace=False)

    probe_type = getattr(module, "AgentRunExecutionProbe", None)
    assert probe_type is not None, "AgentRunExecutionProbe が未実装です"
    return cast(
        "_ExecutionProbe",
        probe_type(
            session_factory,
            run_id,
            attempt_epoch,
            clock=clock,
        ),
    )


async def _repository_is_current(
    repository: AgentRunRepository,
    *,
    run_id: UUID,
    attempt_epoch: int,
) -> bool:
    method = getattr(repository, "is_execution_current", None)
    assert method is not None, "repository の execution存在確認契約が未実装です"
    return await method(run_id=run_id, attempt_epoch=attempt_epoch)


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
async def test_probe_cache_rechecks_at_exactly_two_seconds() -> None:
    factory = FakeSessionFactory([True, False])
    clock = ManualClock()
    probe = _new_probe(factory, clock)

    assert await probe.should_continue() is True
    clock.advance(1.999)
    assert await probe.should_continue() is True
    assert len(factory.sessions) == 1

    clock.now = 2.0
    assert await probe.should_continue() is False
    assert len(factory.sessions) == 2
    assert all(
        isinstance(session, FakeSession) and session.exited == 1
        for session in factory.sessions
    )
    assert all(session.entered == 1 for session in factory.sessions)
    assert all(
        isinstance(session, FakeSession) and session.commit_calls == 0
        for session in factory.sessions
    )


@pytest.mark.asyncio
async def test_false_result_is_terminal_cache_without_later_database_check() -> None:
    factory = FakeSessionFactory([False, True])
    clock = ManualClock()
    probe = _new_probe(factory, clock)

    assert await probe.should_continue() is False
    clock.advance(200.0)
    assert await probe.should_continue() is False

    assert len(factory.sessions) == 1


@pytest.mark.asyncio
async def test_database_check_failure_fails_open_and_recovers_after_cache_window() -> (
    None
):
    factory = FakeSessionFactory([RuntimeError("DB_SECRET"), False])
    clock = ManualClock()
    probe = _new_probe(factory, clock)

    assert await probe.should_continue() is True
    clock.advance(1.999)
    assert await probe.should_continue() is True
    assert len(factory.sessions) == 1

    clock.now = 2.0
    assert await probe.should_continue() is False
    assert len(factory.sessions) == 2
    assert all(
        isinstance(session, FakeSession) and session.exited == 1
        for session in factory.sessions
    )


@pytest.mark.asyncio
async def test_session_open_failure_also_fails_open_and_is_cached() -> None:
    factory = FakeSessionFactory(
        [RuntimeError("SESSION_SECRET"), True],
        fail_on_enter=True,
    )
    clock = ManualClock()
    probe = _new_probe(factory, clock)

    assert await probe.should_continue() is True
    clock.advance(1.0)
    assert await probe.should_continue() is True

    assert len(factory.sessions) == 1


@pytest.mark.asyncio
async def test_repository_uses_narrow_exists_query_without_commit() -> None:
    session = FakeSession(True)
    repository = AgentRunRepository(cast(AsyncSession, session))

    assert (
        await _repository_is_current(
            repository,
            run_id=RUN_ID,
            attempt_epoch=ATTEMPT_EPOCH,
        )
        is True
    )

    assert len(session.statements) == 1
    statement = session.statements[0]
    compile_method = getattr(statement, "compile", None)
    assert compile_method is not None, "existence check はSQL statementで行います"
    compiled = compile_method()
    sql = " ".join(str(compiled).split())
    assert sql.startswith("SELECT 1 FROM agent_runs")
    assert "agent_runs.id =" in sql
    assert "agent_runs.status =" in sql
    assert "agent_runs.attempt_epoch =" in sql
    assert "LIMIT" in sql
    assert "JOIN" not in sql
    assert "agent_runs.thread_id" not in sql
    assert set(compiled.params.values()) >= {
        RUN_ID,
        "running",
        ATTEMPT_EPOCH,
        1,
    }
    assert session.commit_calls == 0


@pytest.mark.asyncio
async def test_unavailable_observation_is_once_per_real_failure_and_pii_free(
    capfire: CaptureLogfire,
) -> None:
    factory = FakeSessionFactory(
        [RuntimeError("DB_EXCEPTION_SECRET"), RuntimeError("DB_EXCEPTION_SECRET")]
    )
    clock = ManualClock()
    probe = _new_probe(factory, clock)
    answer = "ANSWER_SECRET"
    question = "QUESTION_SECRET"
    user_id = "USER_ID_SECRET"

    with capture_logs() as logs:
        assert await probe.should_continue() is True
        clock.advance(1.0)
        assert await probe.should_continue() is True
        clock.advance(1.0)
        assert await probe.should_continue() is True

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
    run = AgentRun(
        thread_id=thread.id,
        user_message_id=user_message.id,
        assistant_message_id=assistant_message_id,
        status=status,
        error_code="internal_error" if status == "failed" else None,
        attempt_epoch=attempt_epoch,
    )
    session.add(run)
    await session.commit()
    await session.refresh(run)
    return run


@pytest.mark.integration
@pytest.mark.asyncio
async def test_actual_postgres_matches_only_running_same_epoch(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as setup_session:
        running = await _create_run(
            setup_session,
            status="running",
            attempt_epoch=3,
        )
        queued = await _create_run(
            setup_session,
            status="queued",
            attempt_epoch=0,
        )
        completed = await _create_run(
            setup_session,
            status="completed",
            attempt_epoch=2,
        )
        failed = await _create_run(
            setup_session,
            status="failed",
            attempt_epoch=2,
        )

    async with session_factory() as session:
        repository = AgentRunRepository(session)
        assert await _repository_is_current(
            repository,
            run_id=running.id,
            attempt_epoch=3,
        )
        assert not await _repository_is_current(
            repository,
            run_id=running.id,
            attempt_epoch=2,
        )
        assert not await _repository_is_current(
            repository,
            run_id=queued.id,
            attempt_epoch=0,
        )
        assert not await _repository_is_current(
            repository,
            run_id=completed.id,
            attempt_epoch=2,
        )
        assert not await _repository_is_current(
            repository,
            run_id=failed.id,
            attempt_epoch=2,
        )
        assert not await _repository_is_current(
            repository,
            run_id=MISSING_RUN_ID,
            attempt_epoch=1,
        )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_actual_cancel_commit_makes_cached_probe_false_after_two_seconds(
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
    assert await probe.should_continue() is True

    async with session_factory() as cancel_session:
        async with cancel_session.begin():
            result = await AgentRunRepository(cancel_session).cancel_run_for_user(
                run_id=run.id,
                user_id=UUID(TEST_USER_ID),
            )
    assert result is not None
    clock.advance(2.0)

    assert await probe.should_continue() is False


@pytest.mark.integration
@pytest.mark.asyncio
async def test_actual_reacquire_makes_old_epoch_probe_false_after_two_seconds(
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
    assert await probe.should_continue() is True

    async with session_factory() as acquire_session:
        async with acquire_session.begin():
            prepared = await AgentRunRepository(acquire_session).acquire_for_execution(
                run.id
            )
    assert prepared is not None
    assert prepared.attempt_epoch == 2
    clock.advance(2.0)

    assert await probe.should_continue() is False
