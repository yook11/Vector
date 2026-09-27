"""同じRunの実行世代が交代したときの回答確定を確認する。"""

import pytest

from app.agent.contract import (
    AnswerPlanSummary,
    AnswerQuestionResult,
    ExternalUrlSource,
)
from app.agent.running.answer_generation import AgentAnswerGenerationRepository
from app.agent.running.attempt_start import AgentRunAttemptStartRepository
from app.agent.running.completion import (
    AgentRunCompletionRepository,
    RunCompletionFailure,
    RunCompletionFailureReason,
    RunCompletionSuccess,
)
from app.agent.running.creation import AgentRunCreationRepository
from local_tests.agent.support import fetch_saved_run_result


@pytest.mark.asyncio
async def test_only_latest_attempt_can_complete_run(
    system_database,
    agent_user_id,
    agent_context,
):
    """同じランを複数の実行が取得した場合、最新世代だけが回答を確定できる"""
    session_factory = agent_context.state.session_factory
    async with session_factory() as session:
        async with session.begin():
            created = await AgentRunCreationRepository(session).create_user_run(
                user_id=agent_user_id,
                question="売上動向を調べてください",
                thread_id=None,
            )
    run_id = created.run_id

    async with session_factory() as session:
        async with session.begin():
            older_attempt_epoch = await AgentRunAttemptStartRepository(
                session
            ).start_run(run_id)

    async with session_factory() as session:
        async with session.begin():
            latest_attempt_epoch = await AgentRunAttemptStartRepository(
                session
            ).start_run(run_id)

    await AgentAnswerGenerationRepository(
        session_factory, run_id, latest_attempt_epoch
    ).start_answer_generation()

    older_attempt_result = AnswerQuestionResult(
        status="answered",
        answer="売上は前年同期比5%減です。[[1]]",
        sources=[
            ExternalUrlSource(
                source_ref="1",
                url="https://example.com/older-attempt-report",
                title="古い世代の売上報告",
                evidence_claim="売上は前年同期比5%減",
                source_name="Older source",
            )
        ],
        missing_aspects=[],
        plan_summary=AnswerPlanSummary(plan_type="search"),
    )
    latest_attempt_result = AnswerQuestionResult(
        status="answered",
        answer="売上は前年同期比10%増です。[[1]]",
        sources=[
            ExternalUrlSource(
                source_ref="1",
                url="https://example.com/latest-attempt-report",
                title="最新世代の売上報告",
                evidence_claim="売上は前年同期比10%増",
                source_name="Latest source",
            )
        ],
        missing_aspects=[],
        plan_summary=AnswerPlanSummary(plan_type="search"),
    )

    # 完了済みの拒否ではなく世代の不一致を検証するため、古い世代から保存を試みる。
    async with session_factory() as session:
        async with session.begin():
            older_attempt_completion = await AgentRunCompletionRepository(
                session
            ).complete_run(
                run_id=run_id,
                result=older_attempt_result,
                expected_attempt_epoch=older_attempt_epoch,
            )
            if isinstance(older_attempt_completion, RunCompletionFailure):
                await session.rollback()

    async with session_factory() as session:
        async with session.begin():
            latest_attempt_completion = await AgentRunCompletionRepository(
                session
            ).complete_run(
                run_id=run_id,
                result=latest_attempt_result,
                expected_attempt_epoch=latest_attempt_epoch,
            )
            if isinstance(latest_attempt_completion, RunCompletionFailure):
                await session.rollback()

    saved_result = await fetch_saved_run_result(
        system_database, run_id=run_id, thread_id=created.thread_id
    )
    assert latest_attempt_epoch > older_attempt_epoch
    assert older_attempt_completion == RunCompletionFailure(
        RunCompletionFailureReason.ATTEMPT_MISMATCH
    )
    assert latest_attempt_completion == RunCompletionSuccess()
    assert saved_result.run["status"] == "completed"
    assert saved_result.run["attempt_epoch"] == latest_attempt_epoch
    assert len(saved_result.assistant_messages) == 1

    saved_answer = {
        "answer": saved_result.assistant_messages[0]["content"],
        "missing_aspects": saved_result.assistant_messages[0]["missing_aspects"],
        "sources": [
            {field: source[field] for field in ExternalUrlSource.model_fields}
            for source in saved_result.sources
        ],
    }
    assert saved_answer == latest_attempt_result.model_dump(
        include={"answer", "missing_aspects", "sources"}
    )
    assert (
        saved_result.run["assistant_message_id"]
        == saved_result.assistant_messages[0]["id"]
    )
    assert (
        saved_result.sources[0]["message_id"]
        == saved_result.run["assistant_message_id"]
    )
