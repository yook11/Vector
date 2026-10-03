"""対象内と対象外をまたいでも、1つのcurationの判定結果が1件だけになることを確認する。"""

import asyncio

import pytest

from app.analysis.assessment.consumer_failure_classification import RetryAssessment
from app.analysis.assessment.service import (
    AssessmentCompletion,
    AssessmentCompletionKind,
)
from app.db.errors import DatabaseConstraintError
from local_tests.assessment.support import (
    assessment_reply,
    build_sqs_record,
    fetch_stored_assessment,
    invoke_sqs_record,
    seed_curation,
    wait_for_blocked_connection,
)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("leader_category", "follower_category", "leader_completion"),
    [
        ("ai", "out_of_scope", AssessmentCompletionKind.IN_SCOPE),
        ("out_of_scope", "ai", AssessmentCompletionKind.OUT_OF_SCOPE),
    ],
)
async def test_concurrent_opposite_results_persist_only_leader_result(
    system_database,
    assessment_runtime,
    gated_ai_responses,
    commit_hold,
    completion_results,
    leader_category,
    follower_category,
    leader_completion,
):
    """対象内と対象外を同時に保存しても、先に処理した結果だけが永続化され、後から処理した結果は永続化されない。"""
    target = await seed_curation(system_database, "https://example.com/opposite")
    record = build_sqs_record(target)
    leader = gated_ai_responses(
        assessment_reply(category=leader_category, investor_take="先行側の判断")
    )
    follower = gated_ai_responses(
        assessment_reply(category=follower_category, investor_take="後続側の判断")
    )
    invocations = []
    try:
        invocations.append(asyncio.create_task(invoke_sqs_record(record)))
        await leader.wait_requested()
        invocations.append(asyncio.create_task(invoke_sqs_record(record)))
        await follower.wait_requested()
        leader.release()
        leader_pid = await commit_hold.wait_inserted()
        follower.release()
        await wait_for_blocked_connection(system_database, leader_pid, invocations)
        commit_hold.release()
        responses = await asyncio.wait_for(
            asyncio.gather(*(asyncio.shield(task) for task in invocations)), 15
        )
    finally:
        leader.release()
        follower.release()
        commit_hold.release()
        await asyncio.wait_for(
            asyncio.gather(
                *(asyncio.shield(task) for task in invocations), return_exceptions=True
            ),
            15,
        )

    assert responses == [
        {"batchItemFailures": []},
        {"batchItemFailures": [{"itemIdentifier": record["messageId"]}]},
    ]
    completions = [
        result
        for result in completion_results
        if isinstance(result, AssessmentCompletion)
    ]
    retries = [
        result for result in completion_results if isinstance(result, RetryAssessment)
    ]
    assert len(completion_results) == 2
    assert [result.kind for result in completions] == [leader_completion]
    assert len(retries) == 1
    assert isinstance(retries[0].error, DatabaseConstraintError)
    stored = await fetch_stored_assessment(system_database, target.curation_id)
    saved = stored.in_scope + stored.out_of_scope
    assert len(saved) == 1
    assert saved[0]["investor_take"] == "先行側の判断"
    assert len(stored.in_scope if leader_category == "ai" else stored.out_of_scope) == 1
    assert len(stored.outbox) == (1 if leader_category == "ai" else 0)
