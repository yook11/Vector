"""回答確定と期限回収が実DBで競合しても、先に確定した結果が維持される。"""

from datetime import timedelta

import pytest

from app.agent.answering.timing import answer_generation_recovery_window
from app.agent.contract import (
    AnswerPlanSummary,
    AnswerQuestionResult,
    ExternalUrlSource,
)
from app.agent.running.completion import (
    RunCompletionFailure,
    RunCompletionFailureReason,
    RunCompletionSuccess,
)
from local_tests.agent.deadline_race_support import (
    pending_answer_completion,
    pending_deadline_recovery,
)
from local_tests.agent.support import (
    create_answering_run,
    fetch_saved_run_result,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("recovery_scope", ["single_run", "all_runs"])
async def test_answer_completion_wins_race_and_preserves_saved_result(
    system_database,
    agent_user_id,
    agent_context,
    owner_session_factory,
    recovery_scope,
):
    """回答確定と期限回収が競合し、回答確定が先に成立した場合、確定結果が変わらない。"""
    session_factory = agent_context.state.session_factory
    run = await create_answering_run(
        owner_session_factory,
        session_factory,
        user_id=agent_user_id,
        question="売上動向を調べてください",
    )
    recovery_deadline = run.answer_started_at + answer_generation_recovery_window()
    answer_result = AnswerQuestionResult(
        status="answered",
        answer="売上は前年同期比10%増です。[[1]]",
        sources=[
            ExternalUrlSource(
                source_ref="1",
                url="https://example.com/sales-report",
                title="売上報告",
                evidence_claim="売上は前年同期比10%増",
                source_name="Example",
            )
        ],
        missing_aspects=[],
        plan_summary=AnswerPlanSummary(plan_type="search"),
    )

    async with pending_answer_completion(
        session_factory,
        run=run,
        answer=answer_result,
        at=recovery_deadline - timedelta(microseconds=1),
    ) as completing:
        completion = await completing.wait_for_result()
        result_before_recovery = await completing.snapshot(
            run_id=run.run_id, thread_id=run.thread_id
        )

        async with pending_deadline_recovery(
            session_factory,
            run_id=run.run_id if recovery_scope == "single_run" else None,
            at=recovery_deadline,
        ) as recovering:
            await recovering.wait_until_blocked_by(completing)
            await completing.finish()
            recovery = await recovering.finish()

    result_after_recovery = await fetch_saved_run_result(
        system_database, run_id=run.run_id, thread_id=run.thread_id
    )

    assert completion == RunCompletionSuccess()
    assert recovery.total_count == 0
    assert result_after_recovery.run["status"] == "completed"
    assert result_after_recovery.run["answer_started_at"] == run.answer_started_at
    assert result_after_recovery == result_before_recovery


@pytest.mark.asyncio
@pytest.mark.parametrize("recovery_scope", ["single_run", "all_runs"])
async def test_deadline_recovery_wins_race_and_rejects_answer(
    system_database,
    agent_user_id,
    agent_context,
    owner_session_factory,
    recovery_scope,
):
    """回答確定と期限回収が競合し、期限回収が先に成立した場合、回答は確定されない。"""
    session_factory = agent_context.state.session_factory
    run = await create_answering_run(
        owner_session_factory,
        session_factory,
        user_id=agent_user_id,
        question="売上動向を調べてください",
    )
    recovery_deadline = run.answer_started_at + answer_generation_recovery_window()
    answer_result = AnswerQuestionResult(
        status="answered",
        answer="売上は前年同期比10%増です。[[1]]",
        sources=[
            ExternalUrlSource(
                source_ref="1",
                url="https://example.com/sales-report",
                title="売上報告",
                evidence_claim="売上は前年同期比10%増",
                source_name="Example",
            )
        ],
        missing_aspects=[],
        plan_summary=AnswerPlanSummary(plan_type="search"),
    )

    async with pending_deadline_recovery(
        session_factory,
        run_id=run.run_id if recovery_scope == "single_run" else None,
        at=recovery_deadline,
    ) as recovering:
        recovery = await recovering.wait_for_result()

        async with pending_answer_completion(
            session_factory, run=run, answer=answer_result, at=recovery_deadline
        ) as completing:
            await completing.wait_until_blocked_by(recovering)
            await recovering.finish()
            completion = await completing.finish()

    result_after_save_attempt = await fetch_saved_run_result(
        system_database, run_id=run.run_id, thread_id=run.thread_id
    )

    assert recovery.total_count == 1
    assert completion == RunCompletionFailure(RunCompletionFailureReason.NOT_RUNNING)
    assert {
        "status": result_after_save_attempt.run["status"],
        "assistant_message_id": result_after_save_attempt.run["assistant_message_id"],
        "answer_started_at": result_after_save_attempt.run["answer_started_at"],
        "answers": result_after_save_attempt.assistant_messages,
        "sources": result_after_save_attempt.sources,
    } == {
        "status": "deadline_exceeded",
        "assistant_message_id": None,
        "answer_started_at": run.answer_started_at,
        "answers": [],
        "sources": [],
    }
