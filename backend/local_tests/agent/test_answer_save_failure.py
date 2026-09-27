"""保存途中の失敗で、未確定の回答と出典が残らないことを確認する。"""

import pytest

from app.queue.messages.agent_run import AgentRunTrigger
from app.queue.tasks.agent_run import run_agent_answer
from local_tests.agent.support import (
    create_user_run,
    fail_after_source_write,
    fetch_saved_run_result,
)


@pytest.mark.asyncio
async def test_answer_save_failure_leaves_no_partial_result(
    system_database,
    agent_user_id,
    agent_context,
    agent_provider_responses,
):
    """回答の保存途中で失敗した場合、回答と出典が部分的に残らない"""
    created = await create_user_run(
        agent_context.state.session_factory,
        user_id=agent_user_id,
        question="売上動向を調べてください",
    )
    agent_provider_responses.answer = "売上は前年同期比10%増です。[[1]]"

    with fail_after_source_write(thread_id=created.thread_id) as written_answer_ids:
        await run_agent_answer(
            trigger=AgentRunTrigger(run_id=created.run_id),
            ctx=agent_context,
        )

    result_after_save_failure = await fetch_saved_run_result(
        system_database,
        run_id=created.run_id,
        thread_id=created.thread_id,
    )

    assert len(written_answer_ids) == 1
    assert result_after_save_failure.run["status"] == "failed"
    assert result_after_save_failure.run["error_code"] == "internal_error"
    assert result_after_save_failure.run["assistant_message_id"] is None
    assert result_after_save_failure.assistant_messages == []
    assert result_after_save_failure.sources == []
