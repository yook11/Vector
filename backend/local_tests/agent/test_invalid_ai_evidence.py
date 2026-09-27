"""AIが返した根拠が不正だった場合の振る舞いを確認する。"""

import pytest

from app.queue.messages.agent_run import AgentRunTrigger
from app.queue.tasks.agent_run import run_agent_answer
from local_tests.agent.support import (
    create_user_run,
    fetch_saved_run_result,
)


@pytest.mark.asyncio
async def test_answer_with_fabricated_citations_is_not_saved(
    system_database,
    agent_user_id,
    agent_context,
    agent_provider_responses,
):
    """AIが捏造した引用を含む回答は保存しない。"""
    created = await create_user_run(
        agent_context.state.session_factory,
        user_id=agent_user_id,
        question="売上動向を調べてください",
    )
    agent_provider_responses.answer = "売上は前年同期比10%増です。[[99]]"

    await run_agent_answer(
        trigger=AgentRunTrigger(run_id=created.run_id), ctx=agent_context
    )

    saved_result = await fetch_saved_run_result(
        system_database, run_id=created.run_id, thread_id=created.thread_id
    )
    assert agent_provider_responses.answer_attempts
    assert saved_result.run["status"] == "failed"
    assert saved_result.run["error_code"] == "generation_unavailable"
    assert saved_result.run["assistant_message_id"] is None
    assert saved_result.assistant_messages == []
    assert saved_result.sources == []


@pytest.mark.asyncio
async def test_invalid_citation_is_regenerated_and_only_accepted_answer_is_saved(
    system_database,
    agent_user_id,
    agent_context,
    agent_provider_responses,
):
    """不正な引用を含む回答は再生成し、採用した回答と出典だけを保存する。"""
    created = await create_user_run(
        agent_context.state.session_factory,
        user_id=agent_user_id,
        question="売上動向を調べてください",
    )
    rejected_answer = "売上は前年同期比5%減です。[[99]]"
    accepted_answer = "売上は前年同期比10%増です。[[1]]"
    agent_provider_responses.answer = [rejected_answer, accepted_answer]

    await run_agent_answer(
        trigger=AgentRunTrigger(run_id=created.run_id), ctx=agent_context
    )

    saved_result = await fetch_saved_run_result(
        system_database, run_id=created.run_id, thread_id=created.thread_id
    )
    assert agent_provider_responses.answer_attempts == [1, 2]
    assert saved_result.run["status"] == "completed"
    assert [message["content"] for message in saved_result.assistant_messages] == [
        accepted_answer
    ]
    assert [
        (source["source_ref"], source["url"]) for source in saved_result.sources
    ] == [("1", "https://example.com/initial-report")]
