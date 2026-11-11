"""AgentRunStateService の開始・完了が期限で止まる契約を実DBで確かめる。"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent.contract import (
    AnswerPlanSummary,
    AnswerQuestionResult,
    ExternalUrlSource,
)
from app.agent.running.finish_notice import ClosedRunningRun
from app.agent.running.state import CannotContinue, RunCompleted
from app.agent.running.types import AgentRunStatus, RunAttempt
from app.models.agent_message import AgentMessage, AgentMessageSource
from app.models.agent_run import AgentRun
from app.models.agent_thread import AgentThread
from app.models.agent_user_daily_quota import AgentUserDailyQuota
from app.shared.web_url import WebUrl
from tests.agent.running._state_service import (
    ClosedRunNotices,
    state_service,
    wait_until_lock_waiting,
)
from tests.conftest import TEST_USER_ID

pytestmark = pytest.mark.integration

_USER_ID = uuid.UUID(TEST_USER_ID)
_USAGE_DATE = date(2026, 9, 2)
_CREATED_AT = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)
_DEADLINE_AT = _CREATED_AT + timedelta(seconds=60)
_BEFORE_DEADLINE = _DEADLINE_AT - timedelta(microseconds=1)
_ANSWER_STARTED_AT = _CREATED_AT + timedelta(seconds=55)
_RECOVERY_DEADLINE = _ANSWER_STARTED_AT + timedelta(seconds=45)
_ORIGINAL_HANDOFF = {"summary": "original handoff"}
_CANDIDATE_HANDOFF = {"summary": "candidate handoff"}


@dataclass(frozen=True, slots=True)
class _SeededRun:
    run_id: uuid.UUID
    thread_id: uuid.UUID


@dataclass(frozen=True, slots=True)
class _PersistedStart:
    status: str
    attempt_epoch: int
    error_code: str | None


@dataclass(frozen=True, slots=True)
class _PersistedComplete:
    status: str
    attempt_epoch: int
    deadline_at: datetime
    answer_started_at: datetime | None
    has_assistant_message: bool
    error_code: str | None
    message_count: int
    source_count: int
    research_handoff: dict[str, Any]


async def _seed_run(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    status: str,
    attempt_epoch: int,
    answer_started_at: datetime | None = None,
) -> _SeededRun:
    async with session_factory() as session:
        if (await session.get(AgentUserDailyQuota, (_USER_ID, _USAGE_DATE))) is None:
            session.add(
                AgentUserDailyQuota(
                    user_id=_USER_ID,
                    usage_date=_USAGE_DATE,
                    used_count=1,
                )
            )
        thread = AgentThread(
            user_id=_USER_ID,
            title="deadline fence",
            research_handoff=_ORIGINAL_HANDOFF,
        )
        session.add(thread)
        await session.flush()
        user_message = AgentMessage(
            thread_id=thread.id,
            seq=1,
            role="user",
            content="deadline fence question",
            missing_aspects=[],
        )
        session.add(user_message)
        await session.flush()
        run = AgentRun(
            thread_id=thread.id,
            user_message_id=user_message.id,
            status=status,
            created_at=_CREATED_AT,
            deadline_at=_DEADLINE_AT,
            answer_started_at=answer_started_at,
            attempt_epoch=attempt_epoch,
            quota_usage_date=_USAGE_DATE,
        )
        session.add(run)
        await session.commit()
        return _SeededRun(run_id=run.id, thread_id=thread.id)


def _answer_result() -> AnswerQuestionResult:
    return AnswerQuestionResult(
        status="answered",
        answer="deadline fence answer [[1]]",
        sources=[
            ExternalUrlSource(
                source_ref="1",
                url=WebUrl("https://example.com/deadline-fence"),
                title="Deadline fence source",
                evidence_claim="Deadline fence evidence.",
                source_name="Example",
            )
        ],
        missing_aspects=[],
        plan_summary=AnswerPlanSummary(plan_type="search"),
    )


async def _load_start_persisted(
    session_factory: async_sessionmaker[AsyncSession],
    run_id: uuid.UUID,
) -> _PersistedStart:
    async with session_factory() as observer:
        run = await observer.get(AgentRun, run_id)
        assert run is not None
        return _PersistedStart(
            status=run.status,
            attempt_epoch=run.attempt_epoch,
            error_code=run.error_code,
        )


async def _load_complete_persisted(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    run_id: uuid.UUID,
    thread_id: uuid.UUID,
) -> _PersistedComplete:
    async with session_factory() as observer:
        run = await observer.get(AgentRun, run_id)
        assert run is not None
        message_count = await observer.scalar(
            select(func.count())
            .select_from(AgentMessage)
            .where(AgentMessage.thread_id == thread_id)
        )
        source_count = await observer.scalar(
            select(func.count())
            .select_from(AgentMessageSource)
            .join(AgentMessage, AgentMessage.id == AgentMessageSource.message_id)
            .where(AgentMessage.thread_id == thread_id)
        )
        research_handoff = await observer.scalar(
            select(AgentThread.research_handoff).where(AgentThread.id == thread_id)
        )
        return _PersistedComplete(
            status=run.status,
            attempt_epoch=run.attempt_epoch,
            deadline_at=run.deadline_at,
            answer_started_at=run.answer_started_at,
            has_assistant_message=run.assistant_message_id is not None,
            error_code=run.error_code,
            message_count=message_count,
            source_count=source_count,
            research_handoff=research_handoff,
        )


@pytest.mark.asyncio
async def test_start_run_starts_only_before_fixed_deadline(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """期限前だけqueued runを開始する。

    境界ちょうどはqueuedのままepochを進めない。
    """
    before = await _seed_run(session_factory, status="queued", attempt_epoch=0)
    at_deadline = await _seed_run(session_factory, status="queued", attempt_epoch=0)

    await state_service(session_factory, now=_BEFORE_DEADLINE).start_attempt(
        before.run_id
    )
    await state_service(session_factory, now=_DEADLINE_AT).start_attempt(
        at_deadline.run_id
    )

    assert (
        await _load_start_persisted(session_factory, before.run_id),
        await _load_start_persisted(session_factory, at_deadline.run_id),
    ) == (
        _PersistedStart(
            status="running",
            attempt_epoch=1,
            error_code=None,
        ),
        _PersistedStart(
            status="queued",
            attempt_epoch=0,
            error_code=None,
        ),
    )


@pytest.mark.asyncio
async def test_complete_run_uses_answer_recovery_deadline(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """元の期限後も新期限前は保存し、新期限ちょうどでは回答を残さない。"""
    accepted_after_original_deadline = await _seed_run(
        session_factory,
        status="running",
        attempt_epoch=3,
        answer_started_at=_ANSWER_STARTED_AT,
    )
    accepted_before_recovery_deadline = await _seed_run(
        session_factory,
        status="running",
        attempt_epoch=3,
        answer_started_at=_ANSWER_STARTED_AT,
    )
    rejected = await _seed_run(
        session_factory,
        status="running",
        attempt_epoch=3,
        answer_started_at=_ANSWER_STARTED_AT,
    )

    notices = ClosedRunNotices()
    accepted_after_original_outcome = await state_service(
        session_factory, notices=notices, now=_CREATED_AT + timedelta(seconds=67)
    ).complete(
        RunAttempt(run_id=accepted_after_original_deadline.run_id, epoch=3),
        result=_answer_result(),
        research_handoff=_CANDIDATE_HANDOFF,
    )
    accepted_before_recovery_outcome = await state_service(
        session_factory,
        notices=notices,
        now=_RECOVERY_DEADLINE - timedelta(microseconds=1),
    ).complete(
        RunAttempt(run_id=accepted_before_recovery_deadline.run_id, epoch=3),
        result=_answer_result(),
        research_handoff=_CANDIDATE_HANDOFF,
    )
    rejected_outcome = await state_service(
        session_factory, notices=notices, now=_RECOVERY_DEADLINE
    ).complete(
        RunAttempt(run_id=rejected.run_id, epoch=3),
        result=_answer_result(),
        research_handoff=_CANDIDATE_HANDOFF,
    )

    assert (
        accepted_after_original_outcome,
        accepted_before_recovery_outcome,
        rejected_outcome,
        notices.closed_runs,
    ) == (
        RunCompleted(),
        RunCompleted(),
        CannotContinue(),
        [
            ClosedRunningRun(
                attempt=RunAttempt(
                    run_id=accepted_after_original_deadline.run_id, epoch=3
                ),
                status=AgentRunStatus.COMPLETED,
                error_code=None,
            ),
            ClosedRunningRun(
                attempt=RunAttempt(
                    run_id=accepted_before_recovery_deadline.run_id, epoch=3
                ),
                status=AgentRunStatus.COMPLETED,
                error_code=None,
            ),
        ],
    )

    assert (
        await _load_complete_persisted(
            session_factory,
            run_id=accepted_after_original_deadline.run_id,
            thread_id=accepted_after_original_deadline.thread_id,
        ),
        await _load_complete_persisted(
            session_factory,
            run_id=accepted_before_recovery_deadline.run_id,
            thread_id=accepted_before_recovery_deadline.thread_id,
        ),
        await _load_complete_persisted(
            session_factory,
            run_id=rejected.run_id,
            thread_id=rejected.thread_id,
        ),
    ) == (
        _PersistedComplete(
            status="completed",
            attempt_epoch=3,
            deadline_at=_DEADLINE_AT,
            answer_started_at=_ANSWER_STARTED_AT,
            has_assistant_message=True,
            error_code=None,
            message_count=2,
            source_count=1,
            research_handoff=_CANDIDATE_HANDOFF,
        ),
        _PersistedComplete(
            status="completed",
            attempt_epoch=3,
            deadline_at=_DEADLINE_AT,
            answer_started_at=_ANSWER_STARTED_AT,
            has_assistant_message=True,
            error_code=None,
            message_count=2,
            source_count=1,
            research_handoff=_CANDIDATE_HANDOFF,
        ),
        _PersistedComplete(
            status="running",
            attempt_epoch=3,
            deadline_at=_DEADLINE_AT,
            answer_started_at=_ANSWER_STARTED_AT,
            has_assistant_message=False,
            error_code=None,
            message_count=1,
            source_count=0,
            research_handoff=_ORIGINAL_HANDOFF,
        ),
    )


@pytest.mark.asyncio
async def test_complete_run_requires_answer_start_without_changing_run(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """回答を開始していない現在の試行の完了は続けられないと返り、Runと回答を変えない。"""
    seeded = await _seed_run(session_factory, status="running", attempt_epoch=3)

    service = state_service(session_factory, now=_CREATED_AT + timedelta(seconds=10))

    outcome = await service.complete(
        RunAttempt(run_id=seeded.run_id, epoch=3),
        result=_answer_result(),
        research_handoff=_CANDIDATE_HANDOFF,
    )

    assert outcome == CannotContinue()
    assert await _load_complete_persisted(
        session_factory,
        run_id=seeded.run_id,
        thread_id=seeded.thread_id,
    ) == _PersistedComplete(
        status="running",
        attempt_epoch=3,
        deadline_at=_DEADLINE_AT,
        answer_started_at=None,
        has_assistant_message=False,
        error_code=None,
        message_count=1,
        source_count=0,
        research_handoff=_ORIGINAL_HANDOFF,
    )


@pytest.mark.asyncio
async def test_complete_run_does_not_change_persisted_result_for_old_epoch(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """古いepochの候補では、保存済みのrunと回答を変えない。"""
    seeded = await _seed_run(
        session_factory,
        status="running",
        attempt_epoch=4,
        answer_started_at=_ANSWER_STARTED_AT,
    )
    before = await _load_complete_persisted(
        session_factory,
        run_id=seeded.run_id,
        thread_id=seeded.thread_id,
    )

    service = state_service(session_factory, now=_CREATED_AT + timedelta(seconds=67))

    outcome = await service.complete(
        RunAttempt(run_id=seeded.run_id, epoch=3),
        result=_answer_result(),
        research_handoff=_CANDIDATE_HANDOFF,
    )

    assert outcome == CannotContinue()
    assert (
        await _load_complete_persisted(
            session_factory,
            run_id=seeded.run_id,
            thread_id=seeded.thread_id,
        )
        == before
    )


@pytest.mark.asyncio
async def test_complete_run_waiting_for_an_unchanged_run_keeps_judgement_before_waiting(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """行をロックしただけで書き換えなかった相手を待つ間に回収期限を越えた完了は、待つ前に判定したとおり回答を保存する。"""
    async with session_factory() as clock_session:
        database_time = await clock_session.scalar(select(func.clock_timestamp()))
    assert isinstance(database_time, datetime)
    answer_started_at = database_time - timedelta(seconds=44)
    recovery_deadline = answer_started_at + timedelta(seconds=45)
    seeded = await _seed_run(
        session_factory,
        status="running",
        attempt_epoch=3,
        answer_started_at=answer_started_at,
    )

    notices = ClosedRunNotices()
    service = state_service(session_factory, notices=notices)
    async with (
        session_factory() as blocker,
        session_factory() as observer,
    ):
        save_task: asyncio.Task[RunCompleted | CannotContinue] | None = None
        try:
            await blocker.begin()
            await blocker.execute(
                select(AgentRun).where(AgentRun.id == seeded.run_id).with_for_update()
            )

            save_task = asyncio.create_task(
                service.complete(
                    RunAttempt(run_id=seeded.run_id, epoch=3),
                    result=_answer_result(),
                    research_handoff=_CANDIDATE_HANDOFF,
                )
            )
            await wait_until_lock_waiting(observer)

            async with asyncio.timeout(2):
                while True:
                    observed_at = await observer.scalar(select(func.clock_timestamp()))
                    assert isinstance(observed_at, datetime)
                    if observed_at >= recovery_deadline:
                        break
                    await asyncio.sleep(0.01)

            await blocker.commit()
            outcome = await save_task
        finally:
            if save_task is not None:
                if not save_task.done():
                    save_task.cancel()
                await asyncio.gather(save_task, return_exceptions=True)
            for session in (blocker, observer):
                if session.in_transaction():
                    await session.rollback()

    assert (outcome, notices.closed_runs) == (
        RunCompleted(),
        [
            ClosedRunningRun(
                attempt=RunAttempt(run_id=seeded.run_id, epoch=3),
                status=AgentRunStatus.COMPLETED,
                error_code=None,
            )
        ],
    )
    persisted = await _load_complete_persisted(
        session_factory,
        run_id=seeded.run_id,
        thread_id=seeded.thread_id,
    )
    assert persisted.status == "completed"
    assert persisted.answer_started_at == answer_started_at
    assert persisted.has_assistant_message is True
    assert persisted.message_count == 2
    assert persisted.source_count == 1
    assert persisted.research_handoff == _CANDIDATE_HANDOFF
