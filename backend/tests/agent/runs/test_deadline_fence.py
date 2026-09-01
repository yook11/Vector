"""Agent run deadline fenceの開始・完了契約。"""

from __future__ import annotations

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
from app.agent.runs.contracts import RunTransitionLostError
from app.agent.runs.repository import AgentRunRepository
from app.models.agent_message import AgentMessage, AgentMessageSource
from app.models.agent_run import AgentRun
from app.models.agent_thread import AgentThread
from app.models.agent_user_daily_quota import AgentUserDailyQuota
from app.shared.security.safe_url import SafeUrl
from tests.conftest import TEST_USER_ID

pytestmark = pytest.mark.integration

_USER_ID = uuid.UUID(TEST_USER_ID)
_USAGE_DATE = date(2026, 9, 2)
_CREATED_AT = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)
_DEADLINE_AT = _CREATED_AT + timedelta(seconds=60)
_BEFORE_DEADLINE = _DEADLINE_AT - timedelta(microseconds=1)
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
    completed_at: datetime | None


@dataclass(frozen=True, slots=True)
class _PersistedComplete:
    status: str
    attempt_epoch: int
    has_assistant_message: bool
    error_code: str | None
    completed_at: datetime | None
    message_count: int
    source_count: int
    research_handoff: dict[str, Any]


async def _seed_run(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    status: str,
    attempt_epoch: int,
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
            started_at=(
                _CREATED_AT + timedelta(seconds=1) if status == "running" else None
            ),
            attempt_epoch=attempt_epoch,
            quota_usage_date=_USAGE_DATE,
        )
        # production model実装前もtest collectionを通し、deadline契約でredにする。
        setattr(run, "deadline_at", _DEADLINE_AT)
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
                url=SafeUrl("https://example.com/deadline-fence"),
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
            completed_at=run.completed_at,
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
            has_assistant_message=run.assistant_message_id is not None,
            error_code=run.error_code,
            completed_at=run.completed_at,
            message_count=message_count,
            source_count=source_count,
            research_handoff=research_handoff,
        )


@pytest.mark.asyncio
async def test_start_run_starts_only_before_fixed_deadline(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """期限前だけqueued runを開始する。

    境界ちょうどはdeadline_exceededのままepochを進めない。
    """
    before = await _seed_run(session_factory, status="queued", attempt_epoch=0)
    at_deadline = await _seed_run(session_factory, status="queued", attempt_epoch=0)

    async with session_factory() as session:
        async with session.begin():
            await AgentRunRepository(session).start_run(
                before.run_id,
                now=_BEFORE_DEADLINE,
            )
            await AgentRunRepository(session).start_run(
                at_deadline.run_id,
                now=_DEADLINE_AT,
            )

    assert (
        await _load_start_persisted(session_factory, before.run_id),
        await _load_start_persisted(session_factory, at_deadline.run_id),
    ) == (
        _PersistedStart(
            status="running",
            attempt_epoch=1,
            error_code=None,
            completed_at=None,
        ),
        _PersistedStart(
            status="deadline_exceeded",
            attempt_epoch=0,
            error_code=None,
            completed_at=_DEADLINE_AT,
        ),
    )


@pytest.mark.asyncio
async def test_complete_run_persists_result_only_before_fixed_deadline(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """期限前のcurrent attemptだけ回答を保存し、境界ちょうどでは候補を残さない。"""
    accepted = await _seed_run(session_factory, status="running", attempt_epoch=3)
    rejected = await _seed_run(session_factory, status="running", attempt_epoch=3)

    async with session_factory() as session:
        async with session.begin():
            await AgentRunRepository(session).complete_run(
                run_id=accepted.run_id,
                result=_answer_result(),
                expected_attempt_epoch=3,
                research_handoff=_CANDIDATE_HANDOFF,
                now=_BEFORE_DEADLINE,
            )
            await AgentRunRepository(session).complete_run(
                run_id=rejected.run_id,
                result=_answer_result(),
                expected_attempt_epoch=3,
                research_handoff=_CANDIDATE_HANDOFF,
                now=_DEADLINE_AT,
            )

    assert (
        await _load_complete_persisted(
            session_factory,
            run_id=accepted.run_id,
            thread_id=accepted.thread_id,
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
            has_assistant_message=True,
            error_code=None,
            completed_at=_BEFORE_DEADLINE,
            message_count=2,
            source_count=1,
            research_handoff=_CANDIDATE_HANDOFF,
        ),
        _PersistedComplete(
            status="deadline_exceeded",
            attempt_epoch=3,
            has_assistant_message=False,
            error_code=None,
            completed_at=_DEADLINE_AT,
            message_count=1,
            source_count=0,
            research_handoff=_ORIGINAL_HANDOFF,
        ),
    )


@pytest.mark.asyncio
async def test_complete_run_does_not_change_persisted_result_for_old_epoch(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """古いepochの候補では、保存済みのrunと回答を変えない。"""
    seeded = await _seed_run(session_factory, status="running", attempt_epoch=4)
    before = await _load_complete_persisted(
        session_factory,
        run_id=seeded.run_id,
        thread_id=seeded.thread_id,
    )

    try:
        async with session_factory() as session:
            async with session.begin():
                await AgentRunRepository(session).complete_run(
                    run_id=seeded.run_id,
                    result=_answer_result(),
                    expected_attempt_epoch=3,
                    research_handoff=_CANDIDATE_HANDOFF,
                    now=_BEFORE_DEADLINE,
                )
    except RunTransitionLostError:
        pass

    assert (
        await _load_complete_persisted(
            session_factory,
            run_id=seeded.run_id,
            thread_id=seeded.thread_id,
        )
        == before
    )
