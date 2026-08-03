"""AgentRunRepository.read_recent_research_checkpoints_for_user の読出し契約

(仕様「注入フロー」節2: 同thread・同user・completed・NOT NULLを新しい順に
最大PRIOR_RESEARCH_CHECKPOINT_LIMIT件だけ返す)。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent.research_checkpoint import (
    PRIOR_RESEARCH_CHECKPOINT_LIMIT,
    ResearchCheckpoint,
    ResearchTaskRecord,
)
from app.agent.runs.repository import AgentRunRepository
from app.models.agent_message import AgentMessage
from app.models.agent_run import AgentRun
from app.models.agent_thread import AgentThread
from tests.conftest import TEST_ADMIN_ID, TEST_USER_ID

pytestmark = pytest.mark.integration

_BASE_COMPLETED_AT = datetime(2026, 8, 1, 9, 0, tzinfo=UTC)


def _checkpoint_json(research_goal: str) -> dict[str, Any]:
    return ResearchCheckpoint(
        as_of=_BASE_COMPLETED_AT,
        tasks=(
            ResearchTaskRecord(
                research_goal=research_goal,
                executed_queries=("q",),
                adopted_claims=(),
            ),
        ),
        unresolved_after_search=(),
    ).model_dump(mode="json")


async def _seed_thread(session: AsyncSession, *, user_id: uuid.UUID) -> uuid.UUID:
    thread = AgentThread(user_id=user_id, title="prior research thread")
    session.add(thread)
    await session.flush()
    return thread.id


async def _seed_run(
    session: AsyncSession,
    *,
    thread_id: uuid.UUID,
    seq: int,
    status: str = "completed",
    completed_at: datetime | None = None,
    research_checkpoint: dict[str, Any] | None = None,
    error_code: str | None = None,
) -> AgentRun:
    user_message = AgentMessage(
        thread_id=thread_id,
        seq=seq,
        role="user",
        content=f"question-{seq}",
        missing_aspects=[],
    )
    session.add(user_message)
    await session.flush()
    assistant_message_id = None
    if status == "completed":
        assistant_message = AgentMessage(
            thread_id=thread_id,
            seq=seq + 1,
            role="assistant",
            content=f"answer-{seq}",
            missing_aspects=[],
        )
        session.add(assistant_message)
        await session.flush()
        assistant_message_id = assistant_message.id
    run = AgentRun(
        thread_id=thread_id,
        user_message_id=user_message.id,
        assistant_message_id=assistant_message_id,
        status=status,
        completed_at=completed_at,
        research_checkpoint=research_checkpoint,
        error_code=error_code,
    )
    session.add(run)
    await session.flush()
    return run


@pytest.mark.asyncio
async def test_returns_completed_checkpoints_newest_first_capped_at_the_shared_limit(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    run_count = PRIOR_RESEARCH_CHECKPOINT_LIMIT + 1
    async with session_factory() as session:
        thread_id = await _seed_thread(session, user_id=uuid.UUID(TEST_USER_ID))
        seq = 1
        for index in range(run_count):
            await _seed_run(
                session,
                thread_id=thread_id,
                seq=seq,
                completed_at=_BASE_COMPLETED_AT + timedelta(hours=index),
                research_checkpoint=_checkpoint_json(f"goal-{index}"),
            )
            seq += 2
        await session.commit()

    async with session_factory() as session:
        raw_checkpoints = await AgentRunRepository(
            session
        ).read_recent_research_checkpoints_for_user(
            thread_id=thread_id,
            user_id=uuid.UUID(TEST_USER_ID),
        )

    assert len(raw_checkpoints) == PRIOR_RESEARCH_CHECKPOINT_LIMIT
    recalled_goals = [
        ResearchCheckpoint.model_validate(raw).tasks[0].research_goal
        for raw in raw_checkpoints
    ]
    # 最新のcompleted_atを持つrun(最後に作った3件)が新しい順で返る。
    assert recalled_goals == [
        f"goal-{run_count - 1}",
        f"goal-{run_count - 2}",
        f"goal-{run_count - 3}",
    ]


@pytest.mark.asyncio
async def test_excludes_checkpoints_from_a_thread_owned_by_another_user(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        foreign_thread_id = await _seed_thread(
            session, user_id=uuid.UUID(TEST_ADMIN_ID)
        )
        await _seed_run(
            session,
            thread_id=foreign_thread_id,
            seq=1,
            completed_at=_BASE_COMPLETED_AT,
            research_checkpoint=_checkpoint_json("foreign-goal"),
        )
        await session.commit()

    async with session_factory() as session:
        raw_checkpoints = await AgentRunRepository(
            session
        ).read_recent_research_checkpoints_for_user(
            thread_id=foreign_thread_id,
            user_id=uuid.UUID(TEST_USER_ID),
        )

    assert raw_checkpoints == []


@pytest.mark.asyncio
async def test_excludes_checkpoints_from_a_different_thread_of_the_same_user(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """thread scopeは同一userでも独立している(注入フロー2: 同thread・同userの両方)。"""
    user_id = uuid.UUID(TEST_USER_ID)
    async with session_factory() as session:
        other_thread_id = await _seed_thread(session, user_id=user_id)
        await _seed_run(
            session,
            thread_id=other_thread_id,
            seq=1,
            completed_at=_BASE_COMPLETED_AT,
            research_checkpoint=_checkpoint_json("other-thread-goal"),
        )
        target_thread_id = await _seed_thread(session, user_id=user_id)
        await session.commit()

    async with session_factory() as session:
        raw_checkpoints = await AgentRunRepository(
            session
        ).read_recent_research_checkpoints_for_user(
            thread_id=target_thread_id,
            user_id=user_id,
        )

    assert raw_checkpoints == []


@pytest.mark.asyncio
async def test_excludes_non_completed_and_null_checkpoint_runs(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        thread_id = await _seed_thread(session, user_id=uuid.UUID(TEST_USER_ID))
        await _seed_run(
            session,
            thread_id=thread_id,
            seq=1,
            status="completed",
            completed_at=_BASE_COMPLETED_AT,
            research_checkpoint=_checkpoint_json("only-eligible-goal"),
        )
        await _seed_run(
            session,
            thread_id=thread_id,
            seq=3,
            status="completed",
            completed_at=_BASE_COMPLETED_AT + timedelta(hours=1),
            research_checkpoint=None,
        )
        await _seed_run(
            session,
            thread_id=thread_id,
            seq=5,
            status="running",
            research_checkpoint=None,
        )
        await _seed_run(
            session,
            thread_id=thread_id,
            seq=7,
            status="failed",
            error_code="internal_error",
            research_checkpoint=None,
        )
        await session.commit()

    async with session_factory() as session:
        raw_checkpoints = await AgentRunRepository(
            session
        ).read_recent_research_checkpoints_for_user(
            thread_id=thread_id,
            user_id=uuid.UUID(TEST_USER_ID),
        )

    assert len(raw_checkpoints) == 1
    assert (
        ResearchCheckpoint.model_validate(raw_checkpoints[0]).tasks[0].research_goal
        == "only-eligible-goal"
    )
