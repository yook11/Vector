"""signalとnoiseをまたいでも、1つの記事の整形結果が1件だけになることを確認する。"""

import asyncio

import pytest

from local_tests.curation.support import (
    build_sqs_record,
    curation_reply,
    fetch_stored_curation,
    invoke_sqs_record,
    seed_article,
    wait_for_blocked_connection,
)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("leader_relevance", "follower_relevance"),
    [("signal", "noise"), ("noise", "signal")],
)
async def test_concurrent_opposite_results_persist_only_leader_result(
    system_database,
    curation_runtime,
    gated_ai_responses,
    commit_hold,
    leader_relevance,
    follower_relevance,
):
    """signalとnoiseを同時に保存しても、先に処理した結果だけが永続化され、後から処理した結果は永続化されない。"""
    target = await seed_article(
        system_database,
        "https://example.com/opposite",
        title="Original title",
        content="Original content",
    )
    record = build_sqs_record(target)
    leader = gated_ai_responses(
        curation_reply(
            relevance=leader_relevance,
            title_ja="先行側の題名",
            summary_ja="先行側の要約",
        )
    )
    follower = gated_ai_responses(
        curation_reply(
            relevance=follower_relevance,
            title_ja="後続側の題名",
            summary_ja="後続側の要約",
        )
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
    stored = await fetch_stored_curation(system_database, target.analyzable_article_id)
    saved = stored.curations + stored.noises
    assert len(saved) == 1
    assert saved[0]["translated_title"] == "先行側の題名"
    assert len(stored.curations if leader_relevance == "signal" else stored.noises) == 1
    assert len(stored.outbox_payloads) == (1 if leader_relevance == "signal" else 0)
