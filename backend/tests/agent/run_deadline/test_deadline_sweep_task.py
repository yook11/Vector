"""期限切れrunを確定するsweep taskの観測契約。"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID

import pytest
from sqlalchemy import event as sa_event
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import app.agent.running.deadline.recovery as deadline_recovery
import app.queue.tasks.agent_run as agent_run_tasks
from app.agent.live_updates.stream import AgentRunLiveStreamTerminalEvent
from app.agent.run_enqueuer import AgentRunTrigger
from app.agent.running.types import RunAttempt
from app.models.agent_run import AgentRun
from app.models.agent_user_daily_quota import AgentUserDailyQuota
from tests.agent._sensitive_db_failure import (
    SensitivePersistenceFailure,
    assert_no_sensitive_marker,
)
from tests.agent.running._seed import (
    create_thread_message_run as _create_thread_message_run,
)
from tests.conftest import TEST_USER_ID


class FakeLiveStreamPublisher:
    instances: list[FakeLiveStreamPublisher] = []
    raise_on_begin = False
    raise_on_publish = False
    publish_outcomes: list[str | None | BaseException] = []

    def __init__(self, redis: object, attempt: RunAttempt) -> None:
        self.redis = redis
        self.run_id = attempt.run_id
        self.attempt_epoch = attempt.epoch
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


@pytest.mark.integration
@pytest.mark.asyncio
async def test_sweep_task_ends_expired_runs_and_notifies_only_running_attempt(
    read_logs,
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """待機中2件と実行中1件が期限切れになると、3件とも時間切れにし、利用枠の日付を持つ待機中の1件分だけ返却し、実行中だった試行にだけ終了を通知する。"""
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

    FakeLiveStreamPublisher.instances = []
    monkeypatch.setattr(
        deadline_recovery,
        "AgentRunLiveStreamPublisher",
        FakeLiveStreamPublisher,
    )

    await agent_run_tasks.sweep_deadline_exceeded_agent_runs(ctx=_ctx(session_factory))
    logs = read_logs()

    assert "sensitive" not in str(logs)
    assert [
        (publisher.run_id, publisher.attempt_epoch)
        for publisher in FakeLiveStreamPublisher.instances
    ] == [(old_running.id, 2)]

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


@pytest.mark.integration
@pytest.mark.asyncio
async def test_sweep_task_emits_no_log_or_notice_when_commit_fails(
    read_logs,
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """回収のcommitが秘匿値つきのDB例外で落ちると、taskは例外を出さずログに秘匿値を残さず、回収のログも終了の通知も出さず、Runは元の状態のまま残る。"""
    deadline_at = datetime.now(UTC) - timedelta(seconds=1)
    async with session_factory() as setup_session:
        _queued_thread, _queued_message, queued = await _create_thread_message_run(
            setup_session,
            question="sensitive rollback queued",
            created_at=deadline_at - timedelta(seconds=60),
            deadline_at=deadline_at,
        )
        _running_thread, _running_message, running = await _create_thread_message_run(
            setup_session,
            question="sensitive rollback running",
            status="running",
            created_at=deadline_at - timedelta(seconds=60),
            deadline_at=deadline_at,
            attempt_epoch=2,
        )

    commit_attempted = False

    def fail_commit(_session: object) -> None:
        nonlocal commit_attempted
        commit_attempted = True
        raise SensitivePersistenceFailure

    failing_session = session_factory()
    sa_event.listen(
        failing_session.sync_session,
        "before_commit",
        fail_commit,
        once=True,
    )

    def failing_session_factory() -> AsyncSession:
        return failing_session

    FakeLiveStreamPublisher.instances = []
    monkeypatch.setattr(
        deadline_recovery,
        "AgentRunLiveStreamPublisher",
        FakeLiveStreamPublisher,
    )

    try:
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
    logs = read_logs()

    assert_no_sensitive_marker(logs)
    assert commit_attempted is True
    assert not [
        entry
        for entry in logs
        if entry.get("event") == "running_deadline_exceeded_swept"
    ]
    assert FakeLiveStreamPublisher.instances == []
    async with session_factory() as verification:
        persisted = [
            await verification.get(AgentRun, run_id)
            for run_id in (queued.id, running.id)
        ]
    assert [run.status if run is not None else None for run in persisted] == [
        "queued",
        "running",
    ]


@pytest.mark.asyncio
async def test_sweep_task_log_output_failure_keeps_committed_sweep(
    request: pytest.FixtureRequest,
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ログの出力が失敗しても、確定した回収は変わらない。"""
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
            attempt_epoch=1,
            quota_usage_date=usage_date,
        )
    monkeypatch.setattr(
        deadline_recovery,
        "AgentRunLiveStreamPublisher",
        FakeLiveStreamPublisher,
    )
    request.getfixturevalue("broken_output")

    await agent_run_tasks.sweep_deadline_exceeded_agent_runs(ctx=_ctx(session_factory))

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
async def test_sweep_task_publishes_each_committed_running_attempt_despite_failure(
    read_logs,
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed_at = datetime.now(UTC)
    deadline_at = observed_at - timedelta(seconds=1)
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
        another_running.answer_started_at = observed_at - timedelta(seconds=60)
        protected_running = (
            await _create_thread_message_run(
                session,
                status="running",
                question="sensitive protected running",
                created_at=deadline_at - timedelta(seconds=60),
                deadline_at=deadline_at,
                attempt_epoch=4,
            )
        )[2]
        protected_running.answer_started_at = observed_at - timedelta(seconds=30)
        await session.commit()

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
        deadline_recovery,
        "AgentRunLiveStreamPublisher",
        CommitCheckingPublisher,
    )

    await agent_run_tasks.sweep_deadline_exceeded_agent_runs(ctx=_ctx(session_factory))
    logs = read_logs()

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
    ] == [
        {
            "count": 2,
            "event": "running_deadline_exceeded_swept",
            "level": "info",
            "log_policy": "research",
        }
    ]
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
    protected = await _persisted_run_for_sweep_test(
        session_factory,
        protected_running.id,
    )
    assert protected.status == "running"


async def _persisted_run_for_sweep_test(
    session_factory: async_sessionmaker[AsyncSession], run_id: UUID
) -> AgentRun:
    async with session_factory() as session:
        run = await session.get(AgentRun, run_id)
    assert run is not None
    return run


@pytest.mark.asyncio
@pytest.mark.parametrize("expired", [False, True])
async def test_single_run_task_checks_queued_deadline(session_factory, expired):
    """queuedは元の期限に到達した場合だけ終了する。"""
    now = datetime.now(UTC)
    async with session_factory() as session:
        _, _, run = await _create_thread_message_run(
            session,
            created_at=now - timedelta(seconds=60),
            deadline_at=now + timedelta(seconds=-1 if expired else 60),
        )
    await agent_run_tasks.check_agent_run_deadline(
        AgentRunTrigger(run_id=run.id), ctx=_ctx(session_factory)
    )
    persisted = await _persisted_run_for_sweep_test(session_factory, run.id)
    assert persisted.status == ("deadline_exceeded" if expired else "queued")


@pytest.mark.asyncio
@pytest.mark.parametrize("expired", [False, True])
async def test_single_run_task_checks_answering_recovery_deadline(
    session_factory, monkeypatch, expired
):
    """runningの回答生成開始後は元の期限を超えても生成後の回収期限で判定する。"""
    from app.agent.answering.timing import (
        ANSWER_GENERATION_RECOVERY_GRACE_SECONDS,
        ANSWER_GENERATION_TIMEOUT_SECONDS,
    )

    now = datetime.now(UTC)
    age = (
        ANSWER_GENERATION_TIMEOUT_SECONDS + ANSWER_GENERATION_RECOVERY_GRACE_SECONDS + 1
        if expired
        else 5
    )
    async with session_factory() as session:
        _, _, run = await _create_thread_message_run(
            session,
            status="running",
            created_at=now - timedelta(seconds=60),
            deadline_at=now - timedelta(seconds=1),
            answer_started_at=now - timedelta(seconds=age),
            attempt_epoch=7,
        )
    monkeypatch.setattr(
        deadline_recovery,
        "AgentRunLiveStreamPublisher",
        MagicMock(return_value=MagicMock(publish=AsyncMock(return_value="terminal"))),
    )
    await agent_run_tasks.check_agent_run_deadline(
        AgentRunTrigger(run_id=run.id), ctx=_ctx(session_factory)
    )
    persisted = await _persisted_run_for_sweep_test(session_factory, run.id)
    assert persisted.status == ("deadline_exceeded" if expired else "running")


@pytest.mark.asyncio
async def test_single_run_task_notifies_current_epoch_after_commit(
    session_factory, monkeypatch
):
    """DBで終了が確定してから、対象runの現在の実行世代を付けて通知する。"""
    at = datetime.now(UTC) - timedelta(seconds=1)
    async with session_factory() as session:
        _, _, run = await _create_thread_message_run(
            session,
            status="running",
            created_at=at - timedelta(seconds=60),
            deadline_at=at,
            attempt_epoch=7,
        )
    observed = []

    async def observe_publish(event):
        persisted = await _persisted_run_for_sweep_test(session_factory, run.id)
        observed.append((persisted.status, event))
        return "terminal"

    publisher = MagicMock(publish=AsyncMock(side_effect=observe_publish))
    factory = MagicMock(return_value=publisher)
    monkeypatch.setattr(deadline_recovery, "AgentRunLiveStreamPublisher", factory)
    ctx = _ctx(session_factory)
    await agent_run_tasks.check_agent_run_deadline(
        AgentRunTrigger(run_id=run.id), ctx=ctx
    )
    factory.assert_called_once_with(
        ctx.state.agent_live_redis, RunAttempt(run_id=run.id, epoch=7)
    )
    assert observed == [
        (
            "deadline_exceeded",
            AgentRunLiveStreamTerminalEvent(status="deadline_exceeded"),
        )
    ]


@pytest.mark.asyncio
async def test_single_run_task_does_not_notify_queued_run(session_factory, monkeypatch):
    """queuedの回収では実行世代への通知を作らない。"""
    at = datetime.now(UTC) - timedelta(seconds=1)
    async with session_factory() as session:
        _, _, run = await _create_thread_message_run(
            session,
            created_at=at - timedelta(seconds=60),
            deadline_at=at,
        )
    factory = MagicMock()
    monkeypatch.setattr(deadline_recovery, "AgentRunLiveStreamPublisher", factory)
    await agent_run_tasks.check_agent_run_deadline(
        AgentRunTrigger(run_id=run.id), ctx=_ctx(session_factory)
    )
    factory.assert_not_called()


@pytest.mark.asyncio
async def test_repeated_deadline_checks_do_not_repeat_terminal_notification(
    session_factory, monkeypatch
):
    """run単位の確認と定期回収を重ねても、終了通知を再送しない。"""
    at = datetime.now(UTC) - timedelta(seconds=1)
    async with session_factory() as session:
        _, _, run = await _create_thread_message_run(
            session,
            status="running",
            attempt_epoch=7,
            created_at=at - timedelta(seconds=60),
            deadline_at=at,
        )
    publisher = MagicMock(publish=AsyncMock(return_value="terminal"))
    monkeypatch.setattr(
        deadline_recovery,
        "AgentRunLiveStreamPublisher",
        MagicMock(return_value=publisher),
    )
    for _ in range(2):
        await agent_run_tasks.check_agent_run_deadline(
            AgentRunTrigger(run_id=run.id), ctx=_ctx(session_factory)
        )
    await agent_run_tasks.sweep_deadline_exceeded_agent_runs(ctx=_ctx(session_factory))
    publisher.publish.assert_awaited_once_with(
        AgentRunLiveStreamTerminalEvent(status="deadline_exceeded")
    )
