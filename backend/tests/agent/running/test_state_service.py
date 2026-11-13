"""AgentRunStateService のDBのやり直しとcommit後の通知(実DB、DB例外は注入)。"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime

import pytest
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent.live_updates.stream import AgentRunLiveStreamTerminalEvent
from app.agent.running.state import CancelledByUser, RunCreated
from app.agent.running.state_service import AgentRunStateService
from app.agent.running.types import AgentRunErrorCode, RunAttempt
from app.db.errors import (
    DatabaseConnectionError,
    DatabaseSyntaxOrAccessError,
    DatabaseTransactionConflictError,
)
from app.models.agent_run import AgentRun
from tests.agent.running._seed import create_thread_message_run
from tests.conftest import TEST_USER_ID

pytestmark = pytest.mark.integration

_USER_ID = uuid.UUID(TEST_USER_ID)


class _AdapterError(Exception):
    """asyncpg のアダプターが元例外に sqlstate を付けた形を模す。"""

    def __init__(self, sqlstate: str) -> None:
        super().__init__("adapter error")
        self.sqlstate = sqlstate


def _db_failure(sqlstate: str) -> DBAPIError:
    return DBAPIError("SELECT agent_runs FOR UPDATE", {}, _AdapterError(sqlstate))


class _FailingSessionFactory:
    """本物のセッションを開き、指定した回数だけ execute か commit で例外を起こす。"""

    def __init__(
        self,
        factory: async_sessionmaker[AsyncSession],
        *,
        execute_failures: tuple[DBAPIError, ...] = (),
        commit_failures: tuple[DBAPIError, ...] = (),
    ) -> None:
        self._factory = factory
        self._execute_failures = list(execute_failures)
        self._commit_failures = list(commit_failures)
        self.opened = 0

    def __call__(self) -> AsyncSession:
        self.opened += 1
        session = self._factory()
        if self._execute_failures:
            execute_failure = self._execute_failures.pop(0)

            async def failing_execute(*_args: object, **_kwargs: object) -> None:
                raise execute_failure

            session.execute = failing_execute  # type: ignore[method-assign]
        elif self._commit_failures:
            commit_failure = self._commit_failures.pop(0)

            async def failing_commit() -> None:
                raise commit_failure

            session.commit = failing_commit  # type: ignore[method-assign]
        return session


@dataclass
class _Publication:
    run_id: uuid.UUID
    attempt_epoch: int
    event: object
    status_at_publish: str


@dataclass
class _RecordingPublishers:
    """終了イベントと、送った時点でDBに見えていたRunの状態を記録する。"""

    session_factory: async_sessionmaker[AsyncSession]
    publications: list[_Publication] = field(default_factory=list)

    def __call__(self, attempt: RunAttempt) -> _RecordingPublisher:
        return _RecordingPublisher(self, attempt.run_id, attempt.epoch)


@dataclass
class _RecordingPublisher:
    owner: _RecordingPublishers
    run_id: uuid.UUID
    attempt_epoch: int

    async def publish(self, event: object) -> None:
        status, _error_code = await _read_run(self.owner.session_factory, self.run_id)
        self.owner.publications.append(
            _Publication(self.run_id, self.attempt_epoch, event, status)
        )


async def _read_run(
    session_factory: async_sessionmaker[AsyncSession], run_id: uuid.UUID
) -> tuple[str, str | None]:
    async with session_factory() as session:
        row = (
            await session.execute(
                select(AgentRun.status, AgentRun.error_code).where(
                    AgentRun.id == run_id
                )
            )
        ).one()
    return row.status, row.error_code


async def _seed_running_run(
    session_factory: async_sessionmaker[AsyncSession],
) -> uuid.UUID:
    async with session_factory() as session:
        _thread, _message, run = await create_thread_message_run(
            session, status="running", attempt_epoch=1
        )
    return run.id


@pytest.mark.asyncio
async def test_transient_failure_before_commit_is_retried_until_the_operation_finishes(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """最初の文が一時的な失敗(deadlock)で落ちても、トランザクションごとやり直して停止を記録する。"""
    run_id = await _seed_running_run(session_factory)
    failing = _FailingSessionFactory(
        session_factory, execute_failures=(_db_failure("40P01"),)
    )
    service = AgentRunStateService(
        session_factory=failing,  # type: ignore[arg-type]
        publisher_for=_RecordingPublishers(session_factory),  # type: ignore[arg-type]
    )

    result = await service.cancel_for_user(run_id, user_id=_USER_ID)

    assert (result, await _read_run(session_factory, run_id), failing.opened) == (
        CancelledByUser(),
        ("failed", AgentRunErrorCode.CANCELLED.value),
        2,
    )


@pytest.mark.asyncio
async def test_failure_not_resolved_by_retry_is_raised_without_retrying(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """やり直しても解消しない失敗(権限不足)は、やり直さずにDBの失敗の種類として送出しRunを変えない。"""
    run_id = await _seed_running_run(session_factory)
    failing = _FailingSessionFactory(
        session_factory, execute_failures=(_db_failure("42501"),)
    )
    service = AgentRunStateService(
        session_factory=failing,  # type: ignore[arg-type]
        publisher_for=_RecordingPublishers(session_factory),  # type: ignore[arg-type]
    )

    with pytest.raises(DatabaseSyntaxOrAccessError):
        await service.cancel_for_user(run_id, user_id=_USER_ID)

    assert (await _read_run(session_factory, run_id), failing.opened) == (
        ("running", None),
        1,
    )


@pytest.mark.asyncio
async def test_transient_failures_stop_being_retried_after_the_attempt_limit(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """一時的な失敗が続くと、最初の1回とやり直し2回で諦めて送出し、Runを変えない。"""
    run_id = await _seed_running_run(session_factory)
    failing = _FailingSessionFactory(
        session_factory,
        execute_failures=(
            _db_failure("40P01"),
            _db_failure("40P01"),
            _db_failure("40P01"),
            _db_failure("40P01"),
        ),
    )
    service = AgentRunStateService(
        session_factory=failing,  # type: ignore[arg-type]
        publisher_for=_RecordingPublishers(session_factory),  # type: ignore[arg-type]
    )

    with pytest.raises(DatabaseTransactionConflictError):
        await service.cancel_for_user(run_id, user_id=_USER_ID)

    assert (await _read_run(session_factory, run_id), failing.opened) == (
        ("running", None),
        3,
    )


@pytest.mark.asyncio
async def test_commit_failure_is_raised_without_retrying_or_notifying(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """commitで接続が切れると書けたか分からないので、やり直さず終了も通知しない。"""
    run_id = await _seed_running_run(session_factory)
    failing = _FailingSessionFactory(
        session_factory, commit_failures=(_db_failure("08006"),)
    )
    publishers = _RecordingPublishers(session_factory)
    service = AgentRunStateService(
        session_factory=failing,  # type: ignore[arg-type]
        publisher_for=publishers,  # type: ignore[arg-type]
    )

    with pytest.raises(DatabaseConnectionError):
        await service.cancel_for_user(run_id, user_id=_USER_ID)

    assert (failing.opened, publishers.publications) == (1, [])


@pytest.mark.asyncio
async def test_closing_operation_notifies_the_attempt_after_commit(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """実行中のRunを止めると、commit済みの停止がDBに見えてから、その世代へ終了を1件通知する。"""
    run_id = await _seed_running_run(session_factory)
    publishers = _RecordingPublishers(session_factory)
    service = AgentRunStateService(
        session_factory=session_factory,
        publisher_for=publishers,  # type: ignore[arg-type]
    )

    await service.cancel_for_user(run_id, user_id=_USER_ID)

    assert publishers.publications == [
        _Publication(
            run_id=run_id,
            attempt_epoch=1,
            event=AgentRunLiveStreamTerminalEvent.model_validate(
                {"status": "failed", "errorCode": "cancelled"}
            ),
            status_at_publish="failed",
        )
    ]


class _RecordingEnqueuer:
    def __init__(self) -> None:
        self.run_ids: list[uuid.UUID] = []

    async def enqueue(self, run_id: uuid.UUID) -> None:
        self.run_ids.append(run_id)


@pytest.mark.asyncio
async def test_create_run_retries_only_the_write_and_enqueues_once(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Runを作る書き込みが一時的な失敗(deadlock)で落ちても、書き込みだけをやり直し、キューへの投入と期限の予約は1回だけ行う。"""
    failing = _FailingSessionFactory(
        session_factory, execute_failures=(_db_failure("40P01"),)
    )
    service = AgentRunStateService(
        session_factory=failing,  # type: ignore[arg-type]
        publisher_for=_RecordingPublishers(session_factory),  # type: ignore[arg-type]
    )
    enqueuer = _RecordingEnqueuer()
    scheduled: list[tuple[uuid.UUID, datetime]] = []

    created = await service.create_run(
        user_id=_USER_ID,
        question="やり直しの質問",
        thread_id=None,
        enqueuer=enqueuer,  # type: ignore[arg-type]
        schedule_deadline_check=lambda run_id, deadline_at: scheduled.append(
            (run_id, deadline_at)
        ),
    )

    async with session_factory() as session:
        saved_run_ids = (await session.execute(select(AgentRun.id))).scalars().all()
    assert isinstance(created, RunCreated)
    assert (enqueuer.run_ids, scheduled, saved_run_ids, failing.opened) == (
        [created.run_id],
        [(created.run_id, created.deadline_at)],
        [created.run_id],
        2,
    )
