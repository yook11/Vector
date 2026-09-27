"""ワーカーから各工程を通し、回答の保存までの振る舞いを確認する。"""

from unittest.mock import patch

import pytest

from app.agent.evidence_collection import EvidenceCollectionService
from app.agent.evidence_collection.external_search.contract import (
    ExternalSearchFailureReason,
    ExternalSearchProviderError,
)
from app.ai_providers.errors import AIProviderNetworkError
from app.queue.messages.agent_run import AgentRunTrigger
from app.queue.tasks.agent_run import run_agent_answer
from local_tests.agent.support import (
    create_user_run,
    fetch_saved_run_result,
)


@pytest.mark.asyncio
async def test_direct_answer_skips_evidence_collection_and_review(
    system_database,
    agent_user_id,
    agent_context,
    agent_provider_responses,
    collect_evidence,
    review_evidence,
):
    """直接回答では、検索・根拠の精査を行わない。"""
    created = await create_user_run(
        agent_context.state.session_factory,
        user_id=agent_user_id,
        question="こんにちは",
    )
    answer = "こんにちは。調べたいニュースについて質問してください。"
    agent_provider_responses.planning_response = {
        "plan_type": "direct_answer",
        "research_tasks": [],
    }
    agent_provider_responses.answer = answer

    await run_agent_answer(
        trigger=AgentRunTrigger(run_id=created.run_id), ctx=agent_context
    )

    saved_result = await fetch_saved_run_result(
        system_database, run_id=created.run_id, thread_id=created.thread_id
    )
    collect_evidence.assert_not_called()
    review_evidence.assert_not_called()
    assert saved_result.run["status"] == "completed"
    assert [message["content"] for message in saved_result.assistant_messages] == [
        answer
    ]
    assert saved_result.sources == []


@pytest.mark.asyncio
async def test_planning_failure_stops_downstream_processing(
    system_database,
    agent_user_id,
    agent_context,
    agent_provider_responses,
):
    """プランナーが失敗したときは後続処理を行わず、回答を保存しない。"""
    created = await create_user_run(
        agent_context.state.session_factory,
        user_id=agent_user_id,
        question="売上動向を調べてください",
    )
    agent_provider_responses.planning_response = AIProviderNetworkError()

    with patch.object(
        EvidenceCollectionService,
        "collect",
        autospec=True,
        side_effect=EvidenceCollectionService.collect,
    ) as collect_evidence:
        await run_agent_answer(
            trigger=AgentRunTrigger(run_id=created.run_id), ctx=agent_context
        )

    saved_result = await fetch_saved_run_result(
        system_database, run_id=created.run_id, thread_id=created.thread_id
    )
    collect_evidence.assert_not_called()
    assert agent_provider_responses.calls == ["question_planner"]
    assert saved_result.run["status"] == "failed"
    assert saved_result.run["error_code"] == "generation_unavailable"
    assert saved_result.run["assistant_message_id"] is None
    assert saved_result.assistant_messages == []
    assert saved_result.sources == []


@pytest.mark.asyncio
async def test_empty_search_results_do_not_stop_answering(
    system_database,
    agent_user_id,
    agent_context,
    agent_provider_responses,
):
    """内部検索と外部検索の根拠がともに0件でも、回答生成と保存まで処理を続ける。"""
    created = await create_user_run(
        agent_context.state.session_factory,
        user_id=agent_user_id,
        question="売上動向を調べてください",
    )
    agent_provider_responses.search_hits = []
    answer = "根拠が見つからなかったため、売上動向は確認できませんでした。"
    agent_provider_responses.answer = answer

    await run_agent_answer(
        trigger=AgentRunTrigger(run_id=created.run_id), ctx=agent_context
    )

    saved_result = await fetch_saved_run_result(
        system_database, run_id=created.run_id, thread_id=created.thread_id
    )
    assert agent_provider_responses.embedding_requests
    assert agent_provider_responses.search_requests
    assert agent_provider_responses.answer_attempts
    assert saved_result.run["status"] == "completed"
    assert [message["content"] for message in saved_result.assistant_messages] == [
        answer
    ]
    assert saved_result.sources == []
    assert (
        "回答に使える根拠を取得できませんでした"
        in saved_result.assistant_messages[0]["missing_aspects"]
    )


@pytest.mark.asyncio
async def test_internal_search_failure_does_not_stop_answering(
    system_database,
    agent_user_id,
    agent_context,
    agent_provider_responses,
):
    """内部検索が失敗しても、回答生成と保存まで処理を続ける。"""
    created = await create_user_run(
        agent_context.state.session_factory,
        user_id=agent_user_id,
        question="売上動向を調べてください",
    )
    agent_provider_responses.embedding_error = AIProviderNetworkError()
    agent_provider_responses.search_hits = []
    answer = "根拠を取得できなかったため、売上動向は確認できませんでした。"
    agent_provider_responses.answer = answer

    await run_agent_answer(
        trigger=AgentRunTrigger(run_id=created.run_id), ctx=agent_context
    )

    saved_result = await fetch_saved_run_result(
        system_database, run_id=created.run_id, thread_id=created.thread_id
    )
    assert agent_provider_responses.embedding_requests
    assert agent_provider_responses.search_requests
    assert agent_provider_responses.answer_attempts == [1]
    assert saved_result.run["status"] == "completed"
    assert [message["content"] for message in saved_result.assistant_messages] == [
        answer
    ]
    assert saved_result.sources == []
    assert (
        "回答に使える根拠を取得できませんでした"
        in saved_result.assistant_messages[0]["missing_aspects"]
    )


@pytest.mark.asyncio
async def test_external_search_failure_does_not_stop_answering(
    system_database,
    agent_user_id,
    agent_context,
    agent_provider_responses,
):
    """外部検索が失敗しても、回答生成と保存まで処理を続ける。"""
    created = await create_user_run(
        agent_context.state.session_factory,
        user_id=agent_user_id,
        question="売上動向を調べてください",
    )
    agent_provider_responses.search_error = ExternalSearchProviderError(
        reason=ExternalSearchFailureReason.HTTP_ERROR
    )
    answer = "根拠を取得できなかったため、売上動向は確認できませんでした。"
    agent_provider_responses.answer = answer

    await run_agent_answer(
        trigger=AgentRunTrigger(run_id=created.run_id), ctx=agent_context
    )

    saved_result = await fetch_saved_run_result(
        system_database, run_id=created.run_id, thread_id=created.thread_id
    )
    assert agent_provider_responses.search_requests
    assert agent_provider_responses.embedding_requests
    assert agent_provider_responses.answer_attempts == [1]
    assert saved_result.run["status"] == "completed"
    assert [message["content"] for message in saved_result.assistant_messages] == [
        answer
    ]
    assert saved_result.sources == []
    assert (
        "回答に使える根拠を取得できませんでした"
        in saved_result.assistant_messages[0]["missing_aspects"]
    )


@pytest.mark.asyncio
async def test_handoff_generation_failure_does_not_fail_the_answer(
    system_database,
    agent_user_id,
    agent_context,
    agent_provider_responses,
):
    """ハンドオフの生成が失敗しても、回答と出典を保存してランを完了する。"""
    created = await create_user_run(
        agent_context.state.session_factory,
        user_id=agent_user_id,
        question="売上動向を調べてください",
    )
    agent_provider_responses.handoff_response = AIProviderNetworkError()
    answer = "売上は前年同期比10%増です。[[1]]"
    agent_provider_responses.answer = answer

    await run_agent_answer(
        trigger=AgentRunTrigger(run_id=created.run_id), ctx=agent_context
    )

    saved_result = await fetch_saved_run_result(
        system_database, run_id=created.run_id, thread_id=created.thread_id
    )
    assert "research_handoff" in agent_provider_responses.calls
    assert saved_result.run["status"] == "completed"
    assert saved_result.run["error_code"] is None
    assert [message["content"] for message in saved_result.assistant_messages] == [
        answer
    ]
    assert [
        (source["source_ref"], source["url"]) for source in saved_result.sources
    ] == [("1", "https://example.com/initial-report")]
