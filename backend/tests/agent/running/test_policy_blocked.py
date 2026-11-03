"""AgentRunStateRepository.record_policy_block の終了契約を実DBで確かめる。"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent.running.daily_quota.release import DailyQuotaReleaseOutcome
from app.agent.running.state import RunFinished, RunRejected, RunRejectionReason
from app.agent.running.state_repository import AgentRunStateRepository
from app.agent.running.types import AgentRunErrorCode, AgentRunStatus
from app.models.agent_message import AgentMessage, AgentMessageSource
from app.models.agent_run import AgentRun
from app.models.agent_thread import AgentThread
from app.models.agent_user_daily_quota import AgentUserDailyQuota
from tests.agent.run_deadline.clock import sweep_deadline_exceeded_runs_at
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
        previous_created_at = _THREAD_UPDATED_AT - timedelta(seconds=1)
        session.add(
            AgentRun(
                thread_id=thread.id,
                user_message_id=previous_question.id,
                assistant_message_id=previous_answer.id,
                status="completed",
                created_at=previous_created_at,
                deadline_at=previous_created_at + timedelta(seconds=60),
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
        created_at = _NOW - timedelta(seconds=1)
        run = AgentRun(
            thread_id=thread.id,
            user_message_id=current_question.id,
            status="running",
            created_at=created_at,
            deadline_at=created_at + timedelta(seconds=60),
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
async def test_record_policy_block_updates_only_the_current_running_attempt(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """現在の世代のポリシー拒否はRunだけをpolicy_blockedで終え、threadと既存の会話を変えない。"""
    seeded = await _seed_running_run(session_factory)

    async with session_factory() as session:
        async with session.begin():
            recorded = await AgentRunStateRepository(session).record_policy_block(
                seeded.run_id,
                attempt_epoch=seeded.attempt_epoch,
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

    assert recorded == RunFinished(
        status=AgentRunStatus.POLICY_BLOCKED,
        error_code=None,
        quota_release_outcome=DailyQuotaReleaseOutcome.NOT_ELIGIBLE,
        running_attempt_epoch=seeded.attempt_epoch,
    )
    assert run is not None
    assert run.status == "policy_blocked"
    assert run.assistant_message_id is None
    assert run.error_code is None
    assert thread is not None and thread.updated_at == _THREAD_UPDATED_AT
    assert messages == [("user", 1), ("assistant", 2), ("user", 3)]
    assert source_count == 1


@pytest.mark.asyncio
async def test_record_policy_block_is_fenced_from_stale_and_terminal_attempts(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """古い世代と終了済みのRunへのポリシー拒否は拒否し、現在の世代だけを終える。"""
    seeded = await _seed_running_run(session_factory)

    async with session_factory() as session:
        async with session.begin():
            repository = AgentRunStateRepository(session)
            stale = await repository.record_policy_block(
                seeded.run_id,
                attempt_epoch=seeded.attempt_epoch - 1,
                now=_NOW,
            )
            current = await repository.record_policy_block(
                seeded.run_id,
                attempt_epoch=seeded.attempt_epoch,
                now=_NOW,
            )
            terminal = await repository.record_policy_block(
                seeded.run_id,
                attempt_epoch=seeded.attempt_epoch,
                now=_NOW,
            )

    assert (stale, current, terminal) == (
        RunRejected(
            reason=RunRejectionReason.ATTEMPT_SUPERSEDED,
            observed_status=AgentRunStatus.RUNNING,
        ),
        RunFinished(
            status=AgentRunStatus.POLICY_BLOCKED,
            error_code=None,
            quota_release_outcome=DailyQuotaReleaseOutcome.NOT_ELIGIBLE,
            running_attempt_epoch=seeded.attempt_epoch,
        ),
        RunRejected(
            reason=RunRejectionReason.ALREADY_FINISHED,
            observed_status=AgentRunStatus.POLICY_BLOCKED,
        ),
    )


@pytest.mark.asyncio
async def test_record_policy_block_does_not_overwrite_a_cancelled_attempt(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """キャンセルで失敗済みのRunへのポリシー拒否は終了済みとして拒否する。"""
    seeded = await _seed_running_run(session_factory)

    async with session_factory() as session:
        async with session.begin():
            repository = AgentRunStateRepository(session)
            assert await repository.record_failure(
                seeded.run_id,
                attempt_epoch=seeded.attempt_epoch,
                error_code=AgentRunErrorCode.CANCELLED,
                now=_NOW,
            ) == RunFinished(
                status=AgentRunStatus.FAILED,
                error_code=AgentRunErrorCode.CANCELLED,
                quota_release_outcome=DailyQuotaReleaseOutcome.NOT_ELIGIBLE,
                running_attempt_epoch=seeded.attempt_epoch,
            )
            recorded = await repository.record_policy_block(
                seeded.run_id,
                attempt_epoch=seeded.attempt_epoch,
                now=_NOW,
            )

    assert recorded == RunRejected(
        reason=RunRejectionReason.ALREADY_FINISHED,
        observed_status=AgentRunStatus.FAILED,
    )


@pytest.mark.asyncio
async def test_policy_blocked_is_excluded_from_restart_and_deadline_sweep(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """policy_blockedのRunは再配信で開始せず、期限回収の対象にもならない。"""
    seeded = await _seed_running_run(session_factory)

    async with session_factory() as session:
        async with session.begin():
            assert await AgentRunStateRepository(session).record_policy_block(
                seeded.run_id,
                attempt_epoch=seeded.attempt_epoch,
                now=_NOW,
            ) == RunFinished(
                status=AgentRunStatus.POLICY_BLOCKED,
                error_code=None,
                quota_release_outcome=DailyQuotaReleaseOutcome.NOT_ELIGIBLE,
                running_attempt_epoch=seeded.attempt_epoch,
            )

    async with session_factory() as session:
        async with session.begin():
            restart_result = await AgentRunStateRepository(session).start_attempt(
                seeded.run_id,
                now=_NOW,
            )
            swept = await sweep_deadline_exceeded_runs_at(session, at=_NOW)

    assert restart_result == RunRejected(
        reason=RunRejectionReason.ALREADY_FINISHED,
        observed_status=AgentRunStatus.POLICY_BLOCKED,
    )
    assert swept.queued_terminal_count == 0
    assert swept.queued_quota_released_count == 0
    assert swept.queued_quota_not_eligible_count == 0
    assert swept.queued_quota_inconsistent_count == 0
    assert swept.running_terminal_runs == ()
    assert swept.running_quota_reservation_count == 0


@pytest.mark.asyncio
async def test_policy_blocked_cancel_is_already_terminal_without_quota_release(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """policy_blockedのRunのキャンセルは終了済みとして拒否し、利用枠を返さない。"""
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
            repository = AgentRunStateRepository(session)
            assert await repository.record_policy_block(
                seeded.run_id,
                attempt_epoch=seeded.attempt_epoch,
                now=_NOW,
            ) == RunFinished(
                status=AgentRunStatus.POLICY_BLOCKED,
                error_code=None,
                quota_release_outcome=DailyQuotaReleaseOutcome.NOT_ELIGIBLE,
                running_attempt_epoch=seeded.attempt_epoch,
            )
            outcome = await repository.cancel_for_user(
                seeded.run_id,
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

    assert outcome == RunRejected(
        reason=RunRejectionReason.ALREADY_FINISHED,
        observed_status=AgentRunStatus.POLICY_BLOCKED,
    )
    assert used_count == 4
