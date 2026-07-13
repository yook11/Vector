"""Agent thread repository tests."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent.threads.repository import AgentThreadRepository
from app.models.agent_message import AgentMessage
from app.models.agent_thread import AgentThread
from tests.conftest import TEST_USER_ID


@pytest.mark.asyncio
async def test_read_recent_messages_projects_missing_aspects_in_bounded_sequence_order(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        thread = AgentThread(
            user_id=UUID(TEST_USER_ID),
            title="target thread",
            updated_at=datetime(2026, 7, 10, tzinfo=UTC),
        )
        other_thread = AgentThread(
            user_id=UUID(TEST_USER_ID),
            title="other thread",
            updated_at=datetime(2026, 7, 10, tzinfo=UTC),
        )
        session.add_all([thread, other_thread])
        await session.flush()
        current_message = AgentMessage(
            thread_id=thread.id,
            seq=5,
            role="user",
            content="current question",
            missing_aspects=[],
        )
        session.add_all(
            [
                AgentMessage(
                    thread_id=thread.id,
                    seq=1,
                    role="user",
                    content="oldest user",
                    missing_aspects=[],
                ),
                AgentMessage(
                    thread_id=thread.id,
                    seq=2,
                    role="assistant",
                    content="included assistant",
                    missing_aspects=["first", "", 42, "second", None, "first"],
                ),
                AgentMessage(
                    thread_id=thread.id,
                    seq=3,
                    role="user",
                    content="included user",
                    missing_aspects=[],
                ),
                AgentMessage(
                    thread_id=thread.id,
                    seq=4,
                    role="assistant",
                    content="latest assistant",
                    missing_aspects=["third"],
                ),
                current_message,
                AgentMessage(
                    thread_id=other_thread.id,
                    seq=1,
                    role="assistant",
                    content="other thread assistant",
                    missing_aspects=["must not leak"],
                ),
            ]
        )
        await session.commit()

    async with session_factory() as session:
        messages = await AgentThreadRepository(session).read_recent_messages_before(
            thread_id=thread.id,
            before_seq=current_message.seq,
            limit=3,
        )

    assert [
        (message.role, message.content, message.missing_aspects) for message in messages
    ] == [
        ("assistant", "included assistant", ("first", "second", "first")),
        ("user", "included user", ()),
        ("assistant", "latest assistant", ("third",)),
    ]
