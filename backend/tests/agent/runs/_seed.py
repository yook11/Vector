"""Agent run テスト用の thread / message / run seed。"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent_message import AgentMessage
from app.models.agent_run import AgentRun
from app.models.agent_thread import AgentThread
from tests.conftest import TEST_USER_ID


async def create_thread_message_run(
    session: AsyncSession,
    *,
    status: str = "queued",
    question: str = "worker question",
    history: list[tuple[str, str] | tuple[str, str, list[object]]] | None = None,
    created_at: datetime | None = None,
    deadline_at: datetime | None = None,
    answer_started_at: datetime | None = None,
    attempt_epoch: int | None = None,
    error_code: str | None = None,
    user_id: str = TEST_USER_ID,
    quota_usage_date: date | None = None,
) -> tuple[AgentThread, AgentMessage, AgentRun]:
    thread = AgentThread(
        user_id=UUID(user_id),
        title="thread",
        updated_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    session.add(thread)
    await session.flush()
    history = history or []
    for seq, entry in enumerate(history, start=1):
        role, content, *missing_aspects = entry
        session.add(
            AgentMessage(
                thread_id=thread.id,
                seq=seq,
                role=role,
                content=content,
                missing_aspects=missing_aspects[0] if missing_aspects else [],
            )
        )
    await session.flush()
    message = AgentMessage(
        thread_id=thread.id,
        seq=len(history) + 1,
        role="user",
        content=question,
        missing_aspects=[],
    )
    session.add(message)
    await session.flush()
    effective_created_at = created_at or datetime.now(UTC)
    run = AgentRun(
        thread_id=thread.id,
        user_message_id=message.id,
        status=status,
        created_at=effective_created_at,
        deadline_at=(
            deadline_at
            if deadline_at is not None
            else effective_created_at + timedelta(seconds=60)
        ),
        answer_started_at=answer_started_at,
        error_code=error_code,
        quota_usage_date=quota_usage_date,
    )
    if attempt_epoch is not None:
        run.attempt_epoch = attempt_epoch
    session.add(run)
    await session.commit()
    await session.refresh(thread)
    await session.refresh(message)
    await session.refresh(run)
    return thread, message, run
