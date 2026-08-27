"""AgentThreadRepository.read_research_handoff_for_user の読出し契約。

正本はthread 1行なので、保証するのは所有権と、未着手threadでNoneになること
だけ。checkpoint時代の「completed runを新しい順にN件」は概念ごと無くなった。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent.research_handoff import (
    ResearchHandoff,
    ResearchRunRecord,
    ResearchTaskRecord,
)
from app.agent.threads.repository import AgentThreadRepository
from app.models.agent_thread import AgentThread
from tests.conftest import TEST_ADMIN_ID, TEST_USER_ID

pytestmark = pytest.mark.integration

_AS_OF = datetime(2026, 8, 1, 9, 0, tzinfo=UTC)


def _handoff_json(research_goal: str) -> dict[str, Any]:
    return ResearchHandoff(
        updated_at=_AS_OF,
        runs=(
            ResearchRunRecord(
                as_of=_AS_OF,
                tasks=(
                    ResearchTaskRecord(
                        research_goal=research_goal,
                        executed_queries=("q",),
                        adopted_claims=(),
                    ),
                ),
                unresolved_after_search=(),
            ),
        ),
    ).model_dump(mode="json")


async def _seed_thread(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    research_handoff: dict[str, Any] | None = None,
) -> uuid.UUID:
    thread = AgentThread(
        user_id=user_id,
        title="research handoff thread",
        research_handoff=research_handoff,
    )
    session.add(thread)
    await session.flush()
    return thread.id


@pytest.mark.asyncio
async def test_returns_the_stored_handoff_for_the_owning_user(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        thread_id = await _seed_thread(
            session,
            user_id=TEST_USER_ID,
            research_handoff=_handoff_json("goal"),
        )
        await session.commit()

    async with session_factory() as session:
        raw_handoff = await AgentThreadRepository(
            session
        ).read_research_handoff_for_user(
            thread_id=thread_id,
            user_id=TEST_USER_ID,
        )

    assert raw_handoff == _handoff_json("goal")


@pytest.mark.asyncio
async def test_returns_none_for_a_thread_that_has_not_researched_yet(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        thread_id = await _seed_thread(session, user_id=TEST_USER_ID)
        await session.commit()

    async with session_factory() as session:
        raw_handoff = await AgentThreadRepository(
            session
        ).read_research_handoff_for_user(
            thread_id=thread_id,
            user_id=TEST_USER_ID,
        )

    assert raw_handoff is None


@pytest.mark.asyncio
async def test_does_not_return_a_handoff_owned_by_another_user(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        thread_id = await _seed_thread(
            session,
            user_id=TEST_ADMIN_ID,
            research_handoff=_handoff_json("other user goal"),
        )
        await session.commit()

    async with session_factory() as session:
        raw_handoff = await AgentThreadRepository(
            session
        ).read_research_handoff_for_user(
            thread_id=thread_id,
            user_id=TEST_USER_ID,
        )

    assert raw_handoff is None
