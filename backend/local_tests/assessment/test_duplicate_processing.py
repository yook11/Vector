"""同じ判定区分の再配送・同時処理で、最初の確定内容を維持することを確認する。"""

import asyncio

import pytest

from app.analysis.assessment.service import AssessmentCompletionKind
from local_tests.assessment.support import (
    build_sqs_record,
    deepseek_reply,
    fetch_stored_assessment,
    invoke_sqs_record,
    seed_curation,
    wait_for_blocked_connection,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("category", ["ai", "out_of_scope"])
async def test_redelivery_preserves_first_result_audit_and_outbox(
    system_database, assessment_runtime, deepseek_response, category
):
    """再配送で異なるAI応答を用意しても初回の結果・監査・Outboxを上書きせず正常終了する。"""
    target = await seed_curation(system_database, "https://example.com/redelivery")
    record = build_sqs_record(target)
    deepseek_response.return_value = deepseek_reply(
        category=category, investor_take="初回の判断"
    )

    assert await invoke_sqs_record(record) == {"batchItemFailures": []}
    first = await fetch_stored_assessment(system_database, target.curation_id)
    assert len(first.in_scope + first.out_of_scope) == 1
    assert (first.in_scope + first.out_of_scope)[0]["investor_take"] == "初回の判断"
    assert len(first.audits) == 1
    assert len(first.outbox) == (1 if category == "ai" else 0)
    deepseek_response.return_value = deepseek_reply(
        category=category, investor_take="再配送時の異なる判断"
    )

    response = await invoke_sqs_record(record)

    assert response == {"batchItemFailures": []}
    assert await fetch_stored_assessment(system_database, target.curation_id) == first


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("category", "completion"),
    [
        ("ai", AssessmentCompletionKind.IN_SCOPE),
        ("out_of_scope", AssessmentCompletionKind.OUT_OF_SCOPE),
    ],
)
async def test_concurrent_saves_keep_leader_result_after_unique_constraint_wait(
    system_database,
    assessment_runtime,
    gated_ai_responses,
    commit_hold,
    completion_results,
    category,
    completion,
):
    """後続のINSERTが実DBの一意制約で待機し、先行側の異なる判定内容を保持してスキップする。"""
    target = await seed_curation(system_database, "https://example.com/concurrent")
    record = build_sqs_record(target)
    leader = gated_ai_responses(
        deepseek_reply(category=category, investor_take="先行側の判断")
    )
    follower = gated_ai_responses(
        deepseek_reply(category=category, investor_take="後続側の異なる判断")
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

    assert responses == [{"batchItemFailures": []}, {"batchItemFailures": []}]
    assert sorted(result.kind for result in completion_results) == sorted(
        [
            completion,
            AssessmentCompletionKind.ALREADY_ASSESSED,
        ]
    )
    stored = await fetch_stored_assessment(system_database, target.curation_id)
    assert len(stored.in_scope + stored.out_of_scope) == 1
    assert (stored.in_scope + stored.out_of_scope)[0]["investor_take"] == "先行側の判断"
    assert len(stored.audits) == 1
    assert stored.audits[0]["event_type"] == "succeeded"
    assert len(stored.outbox) == (1 if category == "ai" else 0)
