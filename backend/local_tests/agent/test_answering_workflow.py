"""ワーカーから各工程を通し、回答の保存までの振る舞いを確認する。"""

from datetime import UTC, datetime
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
    count_cached_query_embeddings,
    create_user_run,
    fetch_saved_run_result,
    read_research_handoff,
    seed_analyzed_article,
)


@pytest.mark.asyncio
async def test_direct_answer_skips_evidence_collection_and_review(
    system_database,
    agent_user_id,
    agent_context,
    owner_session_factory,
    agent_provider_responses,
    collect_evidence,
    review_evidence,
):
    """直接回答では、検索・根拠の精査を行わない。"""
    created = await create_user_run(
        owner_session_factory,
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
    owner_session_factory,
    agent_provider_responses,
):
    """プランナーが失敗したときは後続処理を行わず、回答を保存しない。"""
    created = await create_user_run(
        owner_session_factory,
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
    owner_session_factory,
    agent_provider_responses,
):
    """内部検索と外部検索の根拠がともに0件でも、回答生成と保存まで処理を続ける。"""
    created = await create_user_run(
        owner_session_factory,
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
    owner_session_factory,
    agent_provider_responses,
):
    """内部検索が失敗しても、回答生成と保存まで処理を続ける。"""
    created = await create_user_run(
        owner_session_factory,
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
    owner_session_factory,
    agent_provider_responses,
):
    """外部検索が失敗しても、回答生成と保存まで処理を続ける。"""
    created = await create_user_run(
        owner_session_factory,
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
    owner_session_factory,
    agent_provider_responses,
):
    """ハンドオフの生成が失敗しても、回答と出典を保存してランを完了する。"""
    created = await create_user_run(
        owner_session_factory,
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


@pytest.mark.asyncio
async def test_internal_article_is_saved_as_answer_source(
    system_database,
    agent_user_id,
    agent_context,
    owner_session_factory,
    agent_provider_responses,
):
    """内部検索で見つけた分析済み記事を根拠に回答すると、その記事を出典として保存する。"""
    published_at = datetime(2026, 9, 25, tzinfo=UTC)
    article_id = await seed_analyzed_article(
        system_database, title="売上が前年同期比10%増", published_at=published_at
    )
    created = await create_user_run(
        owner_session_factory,
        user_id=agent_user_id,
        question="売上動向を調べてください",
    )
    agent_provider_responses.search_hits = []
    answer = "売上は前年同期比10%増です。[[1]]"
    agent_provider_responses.answer = answer

    await run_agent_answer(
        trigger=AgentRunTrigger(run_id=created.run_id), ctx=agent_context
    )

    saved_result = await fetch_saved_run_result(
        system_database, run_id=created.run_id, thread_id=created.thread_id
    )
    assert saved_result.run["status"] == "completed"
    assert [message["content"] for message in saved_result.assistant_messages] == [
        answer
    ]
    assert [
        {
            "kind": source["kind"],
            "source_ref": source["source_ref"],
            "analyzed_article_id": source["analyzed_article_id"],
            "url": source["url"],
            "title": source["title"],
            "published_at": source["published_at"],
        }
        for source in saved_result.sources
    ] == [
        {
            "kind": "internal_article",
            "source_ref": "1",
            "analyzed_article_id": article_id,
            "url": None,
            "title": "売上が前年同期比10%増",
            "published_at": published_at,
        }
    ]


@pytest.mark.asyncio
async def test_query_embedding_is_reused_by_next_run(
    system_database,
    agent_user_id,
    agent_context,
    owner_session_factory,
    agent_provider_responses,
):
    """検索語の埋め込みを保存し、同じ検索語のrunでは埋め込みを作り直さない。"""
    agent_provider_responses.answer = "売上は前年同期比10%増です。[[1]]"
    statuses = []
    for _ in range(2):
        created = await create_user_run(
            owner_session_factory,
            user_id=agent_user_id,
            question="売上動向を調べてください",
        )
        await run_agent_answer(
            trigger=AgentRunTrigger(run_id=created.run_id), ctx=agent_context
        )
        saved_result = await fetch_saved_run_result(
            system_database, run_id=created.run_id, thread_id=created.thread_id
        )
        statuses.append(saved_result.run["status"])

    # キャッシュの失敗はrunを失敗させないため、呼び出し回数と保存結果で判定する。
    assert statuses == ["completed", "completed"]
    assert [
        request.queries for request in agent_provider_responses.embedding_requests
    ] == [("売上動向",)]
    assert await count_cached_query_embeddings(system_database) == 1


@pytest.mark.asyncio
async def test_research_handoff_is_saved_to_thread(
    system_database,
    agent_user_id,
    agent_context,
    owner_session_factory,
    agent_provider_responses,
):
    """回答の完了時に、次の質問へ引き継ぐ調査の整理をスレッドへ保存する。"""
    created = await create_user_run(
        owner_session_factory,
        user_id=agent_user_id,
        question="売上動向を調べてください",
    )
    agent_provider_responses.answer = "売上は前年同期比10%増です。[[1]]"

    await run_agent_answer(
        trigger=AgentRunTrigger(run_id=created.run_id), ctx=agent_context
    )

    handoff = await read_research_handoff(system_database, thread_id=created.thread_id)
    assert {
        "collected_overview": handoff["collected_overview"],
        "unresolved_points": handoff["unresolved_points"],
        "next_search_guidance": handoff["next_search_guidance"],
        "research_goals": [
            task["research_goal"] for run in handoff["runs"] for task in run["tasks"]
        ],
    } == {
        "collected_overview": "売上動向を確認した",
        "unresolved_points": "利益率は未確認",
        "next_search_guidance": "利益率を調べる",
        "research_goals": ["売上動向を確認する"],
    }
