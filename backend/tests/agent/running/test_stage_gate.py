"""工程に入る口(AgentRunStageGate)の継続確認と段階の通知。

AgentRunStateService の継続確認を差し替えた unit と、
実DBの Run に対する integration を含み、通知は記録する偽物で受ける。
"""

from __future__ import annotations

from collections import deque
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent.live_updates.reporters import AgentRunLiveStageReporter
from app.agent.live_updates.stream import AgentRunLiveStreamEvent
from app.agent.running.stage_gate import AgentRunStageGate
from app.agent.running.state import (
    AttemptSuperseded,
    CancelledByUser,
    CanContinue,
    CannotContinue,
    DeadlineExceeded,
)
from app.agent.running.state_service import AgentRunStateService
from app.agent.running.types import RunAttempt
from app.db.errors import DatabaseConnectionError, DatabaseConnectionErrorReason
from app.models.agent_run import AgentRun
from tests.agent.running._seed import create_thread_message_run
from tests.agent.running._start_run_outcomes import started_attempt_epoch
from tests.agent.running._state_service import state_service
from tests.conftest import TEST_USER_ID

RUN_ID = UUID("00000000-0000-4000-a000-000000000011")
ATTEMPT_EPOCH = 3


class RecordingPublisher:
    def __init__(self) -> None:
        self.events: list[tuple[UUID, int, AgentRunLiveStreamEvent]] = []

    def publisher_for(self, attempt: RunAttempt) -> _EpochPublisher:
        return _EpochPublisher(self, attempt.run_id, attempt.epoch)

    def published(self) -> list[tuple[UUID, int, dict[str, Any]]]:
        return [
            (run_id, epoch, event.model_dump(mode="json", by_alias=True))
            for run_id, epoch, event in self.events
        ]


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


def _new_gate(
    session_factory: object,
    publisher: RecordingPublisher,
    *,
    run_id: UUID = RUN_ID,
    attempt_epoch: int = ATTEMPT_EPOCH,
) -> AgentRunStageGate:
    attempt = RunAttempt(run_id=run_id, epoch=attempt_epoch)
    return AgentRunStageGate(
        AgentRunStateService(
            session_factory=session_factory,  # type: ignore[arg-type]
            publisher_for=publisher.publisher_for,  # type: ignore[arg-type]
        ),
        attempt,
        stage_reporter=AgentRunLiveStageReporter(
            publisher.publisher_for(attempt)  # type: ignore[arg-type]
        ),
    )


def _unused_session_factory() -> object:
    raise AssertionError("the continuation check is replaced in this test")


def _patch_checks(
    monkeypatch: pytest.MonkeyPatch,
    outcomes: Sequence[CanContinue | CannotContinue | BaseException],
) -> list[None]:
    """継続確認を順に差し替え、呼ばれるたびに1件を記録する。"""
    remaining = deque(outcomes)
    calls: list[None] = []

    async def check(
        self: AgentRunStateService,
        *_args: object,
        **_kwargs: object,
    ) -> CanContinue | CannotContinue:
        calls.append(None)
        item = remaining.popleft()
        if isinstance(item, BaseException):
            raise item
        return item

    monkeypatch.setattr(AgentRunStateService, "authorize_stage_entry", check)
    return calls


@pytest.mark.asyncio
async def test_each_enter_asks_again_without_remembering_earlier_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """続けてよい結果も止まった結果も覚えず、工程に入るたびに確認し、続けてよいときだけ段階を通知する。"""
    calls = _patch_checks(
        monkeypatch, [CanContinue(), AttemptSuperseded(), CanContinue()]
    )
    publisher = RecordingPublisher()
    gate = _new_gate(_unused_session_factory, publisher)

    results = [
        await gate.enter("planning"),
        await gate.enter("evidence_collection"),
        await gate.enter("evidence_review"),
    ]

    assert (results, len(calls), publisher.published()) == (
        [CanContinue(), AttemptSuperseded(), CanContinue()],
        3,
        [
            (RUN_ID, ATTEMPT_EPOCH, {"type": "stage", "stage": "planning"}),
            (RUN_ID, ATTEMPT_EPOCH, {"type": "stage", "stage": "evidence_review"}),
        ],
    )


@pytest.mark.asyncio
async def test_database_failure_is_raised_without_notifying_stage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """継続確認のDBの失敗は続行扱いにせず送出し、段階を通知しない。"""
    failure = DatabaseConnectionError(
        reason=DatabaseConnectionErrorReason.CONNECTION_LOST
    )
    _patch_checks(monkeypatch, [failure])
    publisher = RecordingPublisher()
    gate = _new_gate(_unused_session_factory, publisher)

    with pytest.raises(DatabaseConnectionError) as raised:
        await gate.enter("planning")

    assert (raised.value, publisher.published()) == (failure, [])


async def _seed_running_run(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    deadline_at: datetime | None = None,
) -> UUID:
    async with session_factory() as session:
        _thread, _message, run = await create_thread_message_run(
            session,
            status="running",
            attempt_epoch=1,
            deadline_at=deadline_at,
        )
    return run.id


@pytest.mark.integration
@pytest.mark.asyncio
async def test_current_attempt_continues_and_notifies_stage_once(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """受理期限前の現在の世代が工程に入ると、続けてよいと返してその段階を1件通知する。"""
    run_id = await _seed_running_run(session_factory)
    publisher = RecordingPublisher()
    gate = _new_gate(session_factory, publisher, run_id=run_id, attempt_epoch=1)

    result = await gate.enter("planning")

    assert (result, publisher.published()) == (
        CanContinue(),
        [(run_id, 1, {"type": "stage", "stage": "planning"})],
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_committed_cancel_stops_next_stage_without_notifying(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """利用者の停止がcommitされると、次の工程に入るときに止めたことを返し、その段階を通知しない。"""
    run_id = await _seed_running_run(session_factory)
    publisher = RecordingPublisher()
    gate = _new_gate(session_factory, publisher, run_id=run_id, attempt_epoch=1)
    assert await gate.enter("planning") == CanContinue()

    cancelled = await state_service(session_factory).cancel_for_user(
        run_id,
        user_id=UUID(TEST_USER_ID),
    )
    assert cancelled == CancelledByUser()

    result = await gate.enter("evidence_collection")

    assert (result, publisher.published()) == (
        CancelledByUser(),
        [(run_id, 1, {"type": "stage", "stage": "planning"})],
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_restart_stops_old_attempt_at_next_stage_without_notifying(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """再配信で次の世代が始まると、古い世代は次の工程に入るときに別の試行が担当していると返し、その段階を通知しない。"""
    run_id = await _seed_running_run(session_factory)
    publisher = RecordingPublisher()
    gate = _new_gate(session_factory, publisher, run_id=run_id, attempt_epoch=1)
    assert await gate.enter("planning") == CanContinue()

    attempt_epoch = started_attempt_epoch(
        await state_service(session_factory).start_attempt(run_id)
    )
    assert attempt_epoch == 2

    result = await gate.enter("evidence_collection")

    assert (result, publisher.published()) == (
        AttemptSuperseded(),
        [(run_id, 1, {"type": "stage", "stage": "planning"})],
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_expired_deadline_closes_run_once_and_notifies_only_terminal(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """受理期限を過ぎたRunは工程に入るときに一度だけ期限切れで閉じて終了だけを通知し、以後も期限切れを返す。"""
    run_id = await _seed_running_run(
        session_factory,
        deadline_at=datetime.now(UTC) - timedelta(seconds=1),
    )
    publisher = RecordingPublisher()
    gate = _new_gate(session_factory, publisher, run_id=run_id, attempt_epoch=1)

    results = [await gate.enter("planning"), await gate.enter("evidence_collection")]

    async with session_factory() as session:
        persisted = await session.get(AgentRun, run_id)
    assert persisted is not None
    assert (results, publisher.published()) == (
        [DeadlineExceeded(), DeadlineExceeded()],
        [
            (
                run_id,
                1,
                {"type": "terminal", "status": "deadline_exceeded", "errorCode": None},
            )
        ],
    )
    assert (persisted.status, persisted.attempt_epoch, persisted.error_code) == (
        "deadline_exceeded",
        1,
        None,
    )
