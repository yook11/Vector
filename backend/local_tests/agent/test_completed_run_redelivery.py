"""完了済みRunの再実行防止と、確定結果の保護を確認する。"""

from unittest.mock import patch

import pytest

from app.agent.contract import (
    AnswerPlanSummary,
    AnswerQuestionResult,
    ExternalUrlSource,
)
from app.agent.running.answering_runner import AnsweringRunner
from app.agent.running.completion import (
    AgentRunCompletionRepository,
    RunCompletionFailure,
    RunCompletionFailureReason,
)
from app.agent.running.creation import AgentRunCreationRepository
from app.queue.messages.agent_run import AgentRunTrigger
from app.queue.tasks.agent_run import run_agent_answer
from local_tests.agent.support import fetch_saved_run_result


@pytest.mark.asyncio
async def test_completed_run_is_not_reexecuted_on_redelivery(
    system_database,
    agent_user_id,
    agent_context,
    agent_provider_responses,
):
    """完了済みランは再配送されても再実行しない"""
    async with agent_context.state.session_factory() as session:
        async with session.begin():
            created = await AgentRunCreationRepository(session).create_user_run(
                user_id=agent_user_id,
                question="売上動向を調べてください",
                thread_id=None,
            )
    run_id = created.run_id
    agent_provider_responses.answer = "売上は前年同期比10%増です。[[1]]"

    await run_agent_answer(trigger=AgentRunTrigger(run_id=run_id), ctx=agent_context)

    result_after_initial_execution = await fetch_saved_run_result(
        system_database, run_id=run_id, thread_id=created.thread_id
    )
    assert result_after_initial_execution.run["status"] == "completed"

    with patch.object(
        AnsweringRunner, "run", autospec=True, side_effect=AnsweringRunner.run
    ) as execute_answering:
        await run_agent_answer(
            trigger=AgentRunTrigger(run_id=run_id), ctx=agent_context
        )

    execute_answering.assert_not_called()


@pytest.mark.asyncio
async def test_reexecution_of_completed_run_preserves_saved_result(
    system_database,
    agent_user_id,
    agent_context,
    agent_provider_responses,
):
    """完了済みランが再実行された場合、保存結果が変わらない"""
    async with agent_context.state.session_factory() as session:
        async with session.begin():
            created = await AgentRunCreationRepository(session).create_user_run(
                user_id=agent_user_id,
                question="売上動向を調べてください",
                thread_id=None,
            )
    run_id = created.run_id
    agent_provider_responses.answer = "売上は前年同期比10%増です。[[1]]"

    await run_agent_answer(trigger=AgentRunTrigger(run_id=run_id), ctx=agent_context)

    result_after_initial_execution = await fetch_saved_run_result(
        system_database, run_id=run_id, thread_id=created.thread_id
    )

    reexecution_result = AnswerQuestionResult(
        status="answered",
        answer="売上は前年同期比5%減です。[[1]]",
        sources=[
            ExternalUrlSource(
                source_ref="1",
                url="https://example.com/reexecution-report",
                title="再実行で得た別の売上報告",
                evidence_claim="売上は前年同期比5%減",
                source_name="Another source",
            )
        ],
        missing_aspects=[],
        plan_summary=AnswerPlanSummary(plan_type="search"),
    )

    # 入口の再実行防止で止まらず、保存側の拒否を確認するため直接渡す。
    async with agent_context.state.session_factory() as session:
        async with session.begin():
            completion = await AgentRunCompletionRepository(session).complete_run(
                run_id=run_id,
                result=reexecution_result,
                expected_attempt_epoch=result_after_initial_execution.run[
                    "attempt_epoch"
                ],
            )
            if isinstance(completion, RunCompletionFailure):
                await session.rollback()

    result_after_save_attempt = await fetch_saved_run_result(
        system_database, run_id=run_id, thread_id=created.thread_id
    )
    assert completion == RunCompletionFailure(RunCompletionFailureReason.NOT_RUNNING)
    assert result_after_save_attempt.run["status"] == "completed"
    assert len(result_after_save_attempt.assistant_messages) == 1
    assert (
        result_after_save_attempt.assistant_messages[0]["content"]
        == "売上は前年同期比10%増です。[[1]]"
    )
    assert len(result_after_save_attempt.sources) == 1
    assert result_after_save_attempt == result_after_initial_execution
