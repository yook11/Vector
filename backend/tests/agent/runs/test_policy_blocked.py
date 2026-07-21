"""policy_blocked run terminal のrepository契約。"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent.runs.contracts import CancelRunOutcome
from app.agent.runs.repository import AgentRunRepository
from app.agent.runs.types import AgentRunErrorCode
from app.models.agent_message import AgentMessage, AgentMessageSource
from app.models.agent_run import AgentRun
from app.models.agent_thread import AgentThread
from app.models.agent_user_daily_quota import AgentUserDailyQuota
from tests.conftest import TEST_USER_ID

pytestmark = pytest.mark.integration

_USER_ID = uuid.UUID(TEST_USER_ID)
_NOW = datetime(2026, 7, 20, 12, 0, tzinfo=UTC)
_THREAD_UPDATED_AT = datetime(2026, 7, 1, 0, 0, tzinfo=UTC)
_USAGE_DATE = date(2026, 7, 20)


@dataclass(frozen=True, slots=True)
class _SeededRun:
    run_id: uuid.UUID
    thread_id: uuid.UUID
    attempt_epoch: int


async def _seed_running_run(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    attempt_epoch: int = 3,
    quota_usage_date: date | None = None,
) -> _SeededRun:
    """既存会話を含むrunning runを作り、block branchの非更新対象を観測可能にする。"""
    async with session_factory() as session:
        thread = AgentThread(
            user_id=_USER_ID,
            title="policy blocked repository",
            updated_at=_THREAD_UPDATED_AT,
        )
        session.add(thread)
        await session.flush()
        previous_question = AgentMessage(
            thread_id=thread.id,
            seq=1,
            role="user",
            content="previous question",
            missing_aspects=[],
        )
        previous_answer = AgentMessage(
            thread_id=thread.id,
            seq=2,
            role="assistant",
            content="previous answer",
            missing_aspects=[],
        )
        session.add_all((previous_question, previous_answer))
        await session.flush()
        session.add(
            AgentMessageSource(
                message_id=previous_answer.id,
                ordinal=1,
                kind="external_url",
                source_ref="previous-source",
                url="https://example.com/previous-source",
                title="Previous source",
                evidence_claim="Previous answer evidence.",
            )
        )
        session.add(
            AgentRun(
                thread_id=thread.id,
                user_message_id=previous_question.id,
                assistant_message_id=previous_answer.id,
                status="completed",
                completed_at=_THREAD_UPDATED_AT,
                attempt_epoch=1,
            )
        )
        current_question = AgentMessage(
            thread_id=thread.id,
            seq=3,
            role="user",
            content="current question",
            missing_aspects=[],
        )
        session.add(current_question)
        await session.flush()
        run = AgentRun(
            thread_id=thread.id,
            user_message_id=current_question.id,
            status="running",
            progress_stage="retrieving",
            started_at=_NOW - timedelta(minutes=21),
            attempt_epoch=attempt_epoch,
            quota_usage_date=quota_usage_date,
        )
        session.add(run)
        await session.commit()
        return _SeededRun(
            run_id=run.id,
            thread_id=thread.id,
            attempt_epoch=attempt_epoch,
        )


@pytest.mark.asyncio
async def test_mark_policy_blocked_updates_only_the_current_running_attempt(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    seeded = await _seed_running_run(session_factory)

    async with session_factory() as session:
        async with session.begin():
            changed = await AgentRunRepository(session).mark_policy_blocked(
                seeded.run_id,
                expected_attempt_epoch=seeded.attempt_epoch,
                now=_NOW,
            )

    async with session_factory() as session:
        run = await session.get(AgentRun, seeded.run_id)
        thread = await session.get(AgentThread, seeded.thread_id)
        messages = (
            await session.execute(
                select(AgentMessage.role, AgentMessage.seq)
                .where(AgentMessage.thread_id == seeded.thread_id)
                .order_by(AgentMessage.seq)
            )
        ).all()
        source_count = await session.scalar(
            select(func.count()).select_from(AgentMessageSource)
        )

    assert changed is True
    assert run is not None
    assert (
        run.status,
        run.assistant_message_id,
        run.error_code,
        run.progress_stage,
        run.completed_at,
    ) == ("policy_blocked", None, None, None, _NOW)
    assert thread is not None and thread.updated_at == _THREAD_UPDATED_AT
    assert messages == [("user", 1), ("assistant", 2), ("user", 3)]
    assert source_count == 1


@pytest.mark.asyncio
async def test_mark_policy_blocked_is_fenced_from_stale_and_terminal_attempts(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    seeded = await _seed_running_run(session_factory)

    async with session_factory() as session:
        async with session.begin():
            repository = AgentRunRepository(session)
            stale_changed = await repository.mark_policy_blocked(
                seeded.run_id,
                expected_attempt_epoch=seeded.attempt_epoch - 1,
                now=_NOW,
            )
            current_changed = await repository.mark_policy_blocked(
                seeded.run_id,
                expected_attempt_epoch=seeded.attempt_epoch,
                now=_NOW,
            )
            terminal_changed = await repository.mark_policy_blocked(
                seeded.run_id,
                expected_attempt_epoch=seeded.attempt_epoch,
                now=_NOW + timedelta(seconds=1),
            )

    assert (stale_changed, current_changed, terminal_changed) == (False, True, False)


@pytest.mark.asyncio
async def test_mark_policy_blocked_does_not_overwrite_a_cancelled_attempt(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    seeded = await _seed_running_run(session_factory)

    async with session_factory() as session:
        async with session.begin():
            repository = AgentRunRepository(session)
            assert (
                await repository.mark_failed(
                    seeded.run_id,
                    expected_attempt_epoch=seeded.attempt_epoch,
                    error_code=AgentRunErrorCode.CANCELLED,
                    now=_NOW,
                )
                is True
            )
            changed = await repository.mark_policy_blocked(
                seeded.run_id,
                expected_attempt_epoch=seeded.attempt_epoch,
                now=_NOW,
            )

    assert changed is False


@pytest.mark.asyncio
async def test_policy_blocked_is_excluded_from_reacquire_and_stale_sweep(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    seeded = await _seed_running_run(session_factory)

    async with session_factory() as session:
        async with session.begin():
            repository = AgentRunRepository(session)
            assert (
                await repository.mark_policy_blocked(
                    seeded.run_id,
                    expected_attempt_epoch=seeded.attempt_epoch,
                    now=_NOW,
                )
                is True
            )

    async with session_factory() as session:
        async with session.begin():
            repository = AgentRunRepository(session)
            reacquired = await repository.acquire_for_execution(seeded.run_id, now=_NOW)
            swept = await repository.sweep_stale_runs(now=_NOW)

    assert reacquired is None
    assert (swept.total_count, swept.quota_queued_count, swept.quota_running_count) == (
        0,
        0,
        0,
    )


@pytest.mark.asyncio
async def test_policy_blocked_cancel_is_already_terminal_without_quota_release(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    seeded = await _seed_running_run(
        session_factory,
        quota_usage_date=_USAGE_DATE,
    )
    async with session_factory() as session:
        session.add(
            AgentUserDailyQuota(
                user_id=_USER_ID,
                usage_date=_USAGE_DATE,
                used_count=4,
            )
        )
        await session.commit()

    async with session_factory() as session:
        async with session.begin():
            repository = AgentRunRepository(session)
            assert (
                await repository.mark_policy_blocked(
                    seeded.run_id,
                    expected_attempt_epoch=seeded.attempt_epoch,
                    now=_NOW,
                )
                is True
            )
            outcome = await repository.cancel_run_for_user(
                run_id=seeded.run_id,
                user_id=_USER_ID,
                now=_NOW,
            )

    async with session_factory() as session:
        used_count = await session.scalar(
            select(AgentUserDailyQuota.used_count).where(
                AgentUserDailyQuota.user_id == _USER_ID,
                AgentUserDailyQuota.usage_date == _USAGE_DATE,
            )
        )

    expected_outcome = getattr(CancelRunOutcome, "ALREADY_POLICY_BLOCKED", None)
    assert expected_outcome is not None, (
        "ALREADY_POLICY_BLOCKED must be a cancel outcome"
    )
    assert outcome is not None and outcome.cancel_outcome is expected_outcome
    assert used_count == 4
