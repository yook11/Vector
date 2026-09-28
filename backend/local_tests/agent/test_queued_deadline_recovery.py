"""queuedのまま期限を過ぎたrunの定期回収と、利用枠の返却を確認する。"""

from datetime import timedelta

import pytest
from sqlalchemy import func, select

from app.agent.running.creation import AgentRunCreationRepository
from app.queue.tasks.agent_run import sweep_deadline_exceeded_agent_runs
from local_tests.agent.support import (
    fetch_saved_run_result,
    read_daily_quota_used_counts,
)


@pytest.mark.asyncio
async def test_sweep_returns_quota_of_queued_run_past_deadline(
    system_database,
    agent_user_id,
    agent_context,
    owner_session_factory,
):
    """開始されないまま期限を過ぎたrunを回収すると、予約した利用枠を返却する。"""
    async with owner_session_factory() as session:
        async with session.begin():
            database_time = await session.scalar(select(func.clock_timestamp()))
            created = await AgentRunCreationRepository(session).create_user_run(
                user_id=agent_user_id,
                question="売上動向を調べてください",
                thread_id=None,
                now=database_time - timedelta(minutes=2),
            )
    assert await read_daily_quota_used_counts(
        system_database, user_id=agent_user_id
    ) == [1]

    await sweep_deadline_exceeded_agent_runs(ctx=agent_context)

    saved_result = await fetch_saved_run_result(
        system_database, run_id=created.run_id, thread_id=created.thread_id
    )
    assert saved_result.run["status"] == "deadline_exceeded"
    assert saved_result.assistant_messages == []
    assert await read_daily_quota_used_counts(
        system_database, user_id=agent_user_id
    ) == [0]
