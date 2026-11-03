"""所有ユーザー向けの Run の読み出しを、実 DB に作った Run で確かめる。"""

from __future__ import annotations

from uuid import UUID

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent.live_updates.run_reading import AgentRunLiveRepository
from app.agent.running.types import AgentRunStatus
from tests.agent.runs._seed import (
    create_thread_message_run as _create_thread_message_run,
)
from tests.conftest import TEST_ADMIN_ID, TEST_USER_ID


@pytest.mark.asyncio
async def test_read_live_context_for_user_returns_only_owned_internal_fields(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """本人の Run は状態と実行世代を返し、他人の Run と無い Run は None を返す。"""
    async with session_factory() as setup_session:
        _thread, _message, run = await _create_thread_message_run(
            setup_session,
            status="running",
            attempt_epoch=3,
        )

    async with session_factory() as session:
        repo = AgentRunLiveRepository(session)
        owned = await repo.read_live_context_for_user(
            run_id=run.id,
            user_id=UUID(TEST_USER_ID),
        )
        other_user = await repo.read_live_context_for_user(
            run_id=run.id,
            user_id=UUID(TEST_ADMIN_ID),
        )
        missing = await repo.read_live_context_for_user(
            run_id=UUID("00000000-0000-4000-a000-000000000099"),
            user_id=UUID(TEST_USER_ID),
        )

    assert owned is not None
    assert owned.run_id == run.id
    assert owned.status is AgentRunStatus.RUNNING
    assert owned.attempt_epoch == 3
    assert owned.error_code is None
    assert other_user is None
    assert missing is None


@pytest.mark.asyncio
async def test_read_live_context_for_user_preserves_terminal_error_code(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """取り消しで失敗した Run は、失敗の状態とともにエラーコードを返す。"""
    async with session_factory() as setup_session:
        _thread, _message, run = await _create_thread_message_run(
            setup_session,
            status="failed",
            attempt_epoch=2,
            error_code="cancelled",
        )

    async with session_factory() as session:
        context = await AgentRunLiveRepository(session).read_live_context_for_user(
            run_id=run.id,
            user_id=UUID(TEST_USER_ID),
        )

    assert context is not None
    assert context.status is AgentRunStatus.FAILED
    assert context.attempt_epoch == 2
    assert context.error_code == "cancelled"
