"""queued Run の受理期限による開始の禁止と、開始が利用枠に触れないこと。

実DBで AgentRunStateService の操作から確かめ、返却の観測は記録に差し替える。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from logfire.testing import CaptureLogfire
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import app.agent.run_execution as run_execution
import app.queue.tasks.agent_run as agent_run_tasks
from app.agent.run_enqueuer import AgentRunTrigger
from app.agent.running.deadline.policy import RUN_DEADLINE_SECONDS
from app.agent.running.state import AttemptStarted, CannotContinue
from app.agent.running.types import RunAttempt
from app.models.agent_message import AgentMessage
from app.models.agent_run import AgentRun
from app.models.agent_thread import AgentThread
from app.models.agent_user_daily_quota import AgentUserDailyQuota
from tests.agent.running._state_service import (
    record_closed_runs,
    state_service,
)
from tests.conftest import TEST_USER_ID
from tests.logfire._metric_helpers import collected_metrics

_USER_ID = uuid.UUID(TEST_USER_ID)
_USAGE_DATE = date(2026, 7, 22)
_DB_NOW = datetime(2026, 7, 22, 12, 0, tzinfo=UTC)


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
async def test_queued_start_deadline_starts_just_before_and_refuses_at_boundary(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """期限の直前は開始し、期限ちょうどは開始せず待機中のまま残して利用枠も返さない。"""
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
    assert (exact_result, notices.closed_runs) == (CannotContinue(), [])
    before_run = await _read_run(session_factory, just_before.run_id)
    exact_run = await _read_run(session_factory, exact.run_id)
    assert (before_run.status, exact_run.status) == ("running", "queued")
    assert await _read_counter(session_factory, usage_date=date(2026, 7, 21)) == 1


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
async def test_expired_queued_task_skips_live_provider_and_leaves_run_untouched(
    read_logs,
    session_factory: async_sessionmaker[AsyncSession],
    capfire: CaptureLogfire,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """受理期限を過ぎた待機中のRunのtaskは、配信も回答の工程も作らず、Runと利用枠を変えずに終わる。"""
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
    assert (run.status, run.error_code, run.attempt_epoch) == ("queued", None, 0)
    assert await _read_counter(session_factory, usage_date=_USAGE_DATE) == 1
    assert [
        entry for entry in logs if entry.get("event") == "agent_run_not_started"
    ] == [
        {
            "event": "agent_run_not_started",
            "level": "info",
            "log_policy": "research",
        }
    ]
    assert _quota_release_metric_points(capfire) == []
