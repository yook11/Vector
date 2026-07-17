"""Agent run progress writer tests."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from structlog.testing import capture_logs

from app.agent.runs.progress import AgentRunProgressWriter
from app.models.agent_message import AgentMessage
from app.models.agent_run import AgentRun
from app.models.agent_thread import AgentThread
from tests.conftest import TEST_USER_ID


async def _create_run(
    session: AsyncSession,
    *,
    status: str = "running",
    progress_stage: str | None = None,
    attempt_epoch: int = 1,
) -> AgentRun:
    thread = AgentThread(
        user_id=UUID(TEST_USER_ID),
        title="progress thread",
        updated_at=datetime(2026, 7, 9, tzinfo=UTC),
    )
    session.add(thread)
    await session.flush()
    user_message = AgentMessage(
        thread_id=thread.id,
        seq=1,
        role="user",
        content="progress question",
        missing_aspects=[],
    )
    session.add(user_message)
    await session.flush()
    assistant_message_id = None
    if status == "completed":
        assistant_message = AgentMessage(
            thread_id=thread.id,
            seq=2,
            role="assistant",
            content="answer",
            missing_aspects=[],
        )
        session.add(assistant_message)
        await session.flush()
        assistant_message_id = assistant_message.id
    run = AgentRun(
        thread_id=thread.id,
        user_message_id=user_message.id,
        assistant_message_id=assistant_message_id,
        status=status,
        progress_stage=progress_stage,
        error_code="internal_error" if status == "failed" else None,
        attempt_epoch=attempt_epoch,
    )
    session.add(run)
    await session.commit()
    await session.refresh(run)
    return run


@pytest.mark.asyncio
async def test_progress_writer_updates_running_run(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        run = await _create_run(session)

    writer = AgentRunProgressWriter(session_factory, run.id, run.attempt_epoch)

    await writer.stage_changed("retrieving")

    async with session_factory() as session:
        refreshed = await session.get(AgentRun, run.id)
        assert refreshed is not None
        assert (refreshed.status, refreshed.progress_stage) == ("running", "retrieving")


@pytest.mark.asyncio
async def test_progress_writer_does_not_update_newer_attempt(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        run = await _create_run(
            session,
            progress_stage="planning",
            attempt_epoch=2,
        )

    stale_writer = AgentRunProgressWriter(session_factory, run.id, 1)

    await stale_writer.stage_changed("synthesizing")

    async with session_factory() as session:
        refreshed = await session.get(AgentRun, run.id)
        assert refreshed is not None
        assert (refreshed.attempt_epoch, refreshed.progress_stage) == (2, "planning")


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["completed", "failed"])
async def test_progress_writer_does_not_update_terminal_runs(
    session_factory: async_sessionmaker[AsyncSession],
    status: str,
) -> None:
    async with session_factory() as session:
        run = await _create_run(
            session,
            status=status,
            progress_stage="planning",
        )

    writer = AgentRunProgressWriter(session_factory, run.id, run.attempt_epoch)

    await writer.stage_changed("synthesizing")

    async with session_factory() as session:
        refreshed = await session.get(AgentRun, run.id)
        assert refreshed is not None
        assert (refreshed.status, refreshed.progress_stage) == (status, "planning")


class ExplodingSession:
    async def __aenter__(self) -> Any:
        raise RuntimeError("SHOULD_NOT_LEAK")

    async def __aexit__(self, *args: object) -> None:
        return None


class ExplodingSessionFactory:
    def __call__(self) -> ExplodingSession:
        return ExplodingSession()


@pytest.mark.asyncio
async def test_progress_writer_swallows_exceptions_and_logs_pii_free_warning() -> None:
    run_id = UUID("00000000-0000-4000-a000-000000000010")
    writer = AgentRunProgressWriter(ExplodingSessionFactory(), run_id, 1)  # type: ignore[arg-type]

    with capture_logs() as logs:
        await writer.stage_changed("planning")

    assert len(logs) == 1
    warning = logs[0]
    assert warning["event"] == "agent_run_progress_update_failed"
    assert warning["log_level"] == "warning"
    assert warning["run_id"] == str(run_id)
    assert warning["stage"] == "planning"
    assert "SHOULD_NOT_LEAK" not in repr(warning)
