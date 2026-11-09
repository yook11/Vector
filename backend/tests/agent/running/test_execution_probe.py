"""Agent run execution continuation probe の契約。

AgentRunStateService の継続確認を差し替えた unit と、
実DBの Run に対する integration を含む。
"""

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

from app.agent.live_updates.stream import AgentRunLiveStreamEvent
from app.agent.running.continuation import AgentRunExecutionProbe
from app.agent.running.state import (
    AttemptSuperseded,
    CancelledByUser,
    CanContinue,
    CannotContinue,
    DeadlineExceeded,
)
from app.agent.running.state_repository import AgentRunStateRepository
from app.agent.running.state_service import AgentRunStateService
from app.agent.running.stop import AnswerGenerationStopped
from app.models.agent_message import AgentMessage
from app.models.agent_run import AgentRun
from app.models.agent_thread import AgentThread
from tests.agent.running._start_run_outcomes import started_attempt_epoch
from tests.conftest import TEST_USER_ID
from tests.logfire._metric_helpers import collected_metrics

RUN_ID = UUID("00000000-0000-4000-a000-000000000011")
ATTEMPT_EPOCH = 3
UNAVAILABLE_METRIC = "vector.agent.execution_probe.unavailable"


@dataclass
class ManualClock:
    now: float = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class RaisingSessionContext:
    def __init__(self, exception: BaseException) -> None:
        self._exception = exception

    async def __aenter__(self) -> None:
        raise self._exception

    async def __aexit__(self, *args: object) -> None:
        return None


class RaisingSessionFactory:
    def __init__(self, exception: BaseException) -> None:
        self._exception = exception
        self.opened = 0

    def __call__(self) -> RaisingSessionContext:
        self.opened += 1
        return RaisingSessionContext(self._exception)


class RecordingPublisher:
    def __init__(self) -> None:
        self.events: list[tuple[UUID, int, AgentRunLiveStreamEvent]] = []

    def publisher_for(self, run_id: UUID, attempt_epoch: int) -> _EpochPublisher:
        return _EpochPublisher(self, run_id, attempt_epoch)


class _EpochPublisher:
    def __init__(
        self, recorder: RecordingPublisher, run_id: UUID, attempt_epoch: int
    ) -> None:
        self._recorder = recorder
        self._run_id = run_id
        self._attempt_epoch = attempt_epoch

    async def publish(self, event: AgentRunLiveStreamEvent) -> str | None:
        self._recorder.events.append((self._run_id, self._attempt_epoch, event))
        return None


def _new_probe(
    session_factory: object,
    clock: ManualClock,
    *,
    run_id: UUID = RUN_ID,
    attempt_epoch: int = ATTEMPT_EPOCH,
    publisher: RecordingPublisher | None = None,
) -> AgentRunExecutionProbe:
    recorder = publisher or RecordingPublisher()
    return AgentRunExecutionProbe(
        AgentRunStateService(
            session_factory=cast("async_sessionmaker[AsyncSession]", session_factory),
            publisher_for=recorder.publisher_for,  # type: ignore[arg-type]
        ),
        run_id,
        attempt_epoch,
        clock=clock,
    )


def _unused_session_factory() -> object:
    raise AssertionError("the continuation check is replaced in this test")


@dataclass
class ScriptedChecks:
    """継続確認の差し替え。呼ばれた回数を数える。"""

    outcomes: deque[CanContinue | CannotContinue | BaseException]
    calls: int = 0


def _patch_checks(
    monkeypatch: pytest.MonkeyPatch,
    outcomes: Sequence[CanContinue | CannotContinue | BaseException],
) -> ScriptedChecks:
    checks = ScriptedChecks(outcomes=deque(outcomes))

    async def check(
        self: AgentRunStateService,
        *_args: object,
        **_kwargs: object,
    ) -> CanContinue | CannotContinue:
        checks.calls += 1
        item = checks.outcomes.popleft()
        if isinstance(item, BaseException):
            raise item
        return item

    monkeypatch.setattr(AgentRunStateService, "check_continuation", check)
    return checks


async def _stopped(probe: AgentRunExecutionProbe) -> CannotContinue:
    with pytest.raises(AnswerGenerationStopped) as stopped:
        await probe.ensure_can_continue()
    return stopped.value.stopped


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
    """前回の確認から2秒未満は確認を省き、2秒ちょうどで再確認して止める。"""
    checks = _patch_checks(monkeypatch, [CanContinue(), AttemptSuperseded()])
    clock = ManualClock()
    probe = _new_probe(_unused_session_factory, clock)

    await probe.ensure_can_continue()
    clock.advance(1.999)
    await probe.ensure_can_continue()
    assert checks.calls == 1

    clock.now = 2.0
    assert await _stopped(probe) == AttemptSuperseded()
    assert checks.calls == 2


@pytest.mark.asyncio
async def test_stop_result_is_terminal_cache_without_later_database_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """一度止める結果を得たら、時間が経っても確認し直さず同じ結果で止め続ける。"""
    checks = _patch_checks(monkeypatch, [AttemptSuperseded(), CanContinue()])
    clock = ManualClock()
    probe = _new_probe(_unused_session_factory, clock)

    assert await _stopped(probe) == AttemptSuperseded()
    clock.advance(200.0)
    assert await _stopped(probe) == AttemptSuperseded()
    assert checks.calls == 1


@pytest.mark.asyncio
async def test_database_check_failure_fails_open_and_recovers_after_cache_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """継続確認が失敗したら続行させ、2秒後の再確認で止める結果を得れば止める。"""
    checks = _patch_checks(
        monkeypatch, [RuntimeError("DB_SECRET"), AttemptSuperseded()]
    )
    clock = ManualClock()
    probe = _new_probe(_unused_session_factory, clock)

    await probe.ensure_can_continue()
    clock.advance(1.999)
    await probe.ensure_can_continue()
    assert checks.calls == 1

    clock.now = 2.0
    assert await _stopped(probe) == AttemptSuperseded()
    assert checks.calls == 2


@pytest.mark.asyncio
async def test_session_open_failure_also_fails_open_and_is_cached() -> None:
    """セッションを開けない継続確認も続行させ、2秒以内は開き直さない。"""
    factory = RaisingSessionFactory(RuntimeError("SESSION_SECRET"))
    clock = ManualClock()
    probe = _new_probe(factory, clock)

    await probe.ensure_can_continue()
    clock.advance(1.0)
    await probe.ensure_can_continue()
    assert factory.opened == 1


@pytest.mark.asyncio
async def test_unavailable_observation_is_once_per_real_failure_and_pii_free(
    read_logs,
    capfire: CaptureLogfire,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_checks(
        monkeypatch,
        [RuntimeError("DB_EXCEPTION_SECRET"), RuntimeError("DB_EXCEPTION_SECRET")],
    )
    clock = ManualClock()
    probe = _new_probe(_unused_session_factory, clock)
    answer = "ANSWER_SECRET"
    question = "QUESTION_SECRET"
    user_id = "USER_ID_SECRET"

    await probe.ensure_can_continue()
    clock.advance(1.0)
    await probe.ensure_can_continue()
    clock.advance(1.0)
    await probe.ensure_can_continue()
    logs = read_logs()

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
    """キャンセルがcommitされると、2秒後の再確認で利用者が止めたとして止まる。"""
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
    await probe.ensure_can_continue()

    async with session_factory() as cancel_session:
        async with cancel_session.begin():
            result = await AgentRunStateRepository(cancel_session).cancel_for_user(
                run.id,
                user_id=UUID(TEST_USER_ID),
            )
    assert result == CancelledByUser()
    clock.advance(2.0)

    assert await _stopped(probe) == CancelledByUser()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_actual_restart_makes_old_epoch_probe_stop_after_two_seconds(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """再配信で次の世代が始まると、古い世代のprobeは2秒後の再確認で別の試行が担当しているとして止まる。"""
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
    await probe.ensure_can_continue()

    async with session_factory() as start_session:
        async with start_session.begin():
            attempt_epoch = started_attempt_epoch(
                await AgentRunStateRepository(start_session).start_attempt(run.id)
            )
    assert attempt_epoch == 2
    clock.advance(2.0)

    assert await _stopped(probe) == AttemptSuperseded()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_expired_deadline_stops_through_choke_and_writes_once(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """受理期限を過ぎたRunはprobeの確認で一度だけ期限切れに書き換わって終了が通知され、以後も同じ結果で止まる。"""
    async with session_factory() as setup_session:
        run = await _create_run(
            setup_session,
            status="running",
            attempt_epoch=1,
            deadline_at=datetime.now(UTC) - timedelta(seconds=1),
        )
    clock = ManualClock()
    publisher = RecordingPublisher()
    probe = _new_probe(
        session_factory,
        clock,
        run_id=run.id,
        attempt_epoch=1,
        publisher=publisher,
    )
    assert await _stopped(probe) == DeadlineExceeded()
    clock.advance(200.0)
    assert await _stopped(probe) == DeadlineExceeded()
    assert [
        (run_id, epoch, event.model_dump(mode="json", by_alias=True))
        for run_id, epoch, event in publisher.events
    ] == [
        (
            run.id,
            1,
            {"type": "terminal", "status": "deadline_exceeded", "errorCode": None},
        )
    ]

    async with session_factory() as session:
        persisted = await session.get(AgentRun, run.id)
    assert persisted is not None
    assert persisted.status == "deadline_exceeded"
    assert persisted.attempt_epoch == 1
    assert persisted.error_code is None
