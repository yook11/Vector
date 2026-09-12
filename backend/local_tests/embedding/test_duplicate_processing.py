"""同じ記事を指すイベントが重複配送・同時処理されても、保存は1回だけで両方が正常終了することを確認する。"""

import asyncio
import json

import httpx
import pytest

from app.analysis.embedding.service import EmbeddingCompletion
from local_tests.embedding.support import (
    build_sqs_record,
    fetch_stored_embedding,
    invoke_sqs_record,
    seed_article,
)

ROW_LOCK_TIMEOUT = 3.0
INVOCATION_TIMEOUT = 15


@pytest.mark.asyncio
async def test_redelivered_event_completes_without_overwriting_saved_embedding(
    system_database, embedding_runtime, gemini_response
):
    """初回は保存に成功し、重複配送はAI生成に到達せず正常終了し、保存済みの結果は変わらない。"""
    article = await seed_article(system_database, "https://example.com/redelivery")
    record = build_sqs_record(article)
    first_vector = [(index - 384) / 512 for index in range(768)]
    # 重複配送で誤って上書きすると気づけるように、初回とは異なるAI応答を用意する。
    redelivered_vector = [-value for value in first_vector]
    gemini_response.return_value = httpx.Response(
        200, json={"embeddings": [{"values": first_vector}]}
    )

    response = await invoke_sqs_record(record)

    assert response == {"batchItemFailures": []}
    assert gemini_response.await_count == 1
    stored_after_first_delivery = await fetch_stored_embedding(
        system_database, article.analyzed_article_id
    )
    assert stored_after_first_delivery is not None
    assert json.loads(stored_after_first_delivery) == pytest.approx(
        first_vector, abs=0.001
    )

    gemini_response.return_value = httpx.Response(
        200, json={"embeddings": [{"values": redelivered_vector}]}
    )

    response = await invoke_sqs_record(record)

    assert response == {"batchItemFailures": []}
    assert gemini_response.await_count == 1
    stored_after_redelivery = await fetch_stored_embedding(
        system_database, article.analyzed_article_id
    )
    assert stored_after_redelivery == stored_after_first_delivery


async def _wait_until_follower_blocked_by_row_lock(database, leader_pid, invocations):
    """後続側が先行側の行ロックで待たされる状態を実DBで確認する。"""
    deadline = asyncio.get_running_loop().time() + ROW_LOCK_TIMEOUT
    async with database.connect("vector_app") as connection:
        while True:
            assert all(not task.done() for task in invocations), (
                "行ロックで待たされる前に呼び出しが終了した"
            )
            blocked = await connection.fetchval(
                "SELECT EXISTS (SELECT 1 FROM pg_stat_activity "
                "WHERE datname = current_database() "
                "AND application_name = 'vector-embedding-consumer' "
                "AND wait_event_type = 'Lock' "
                "AND $1::integer = ANY(pg_blocking_pids(pid)))",
                leader_pid,
            )
            if blocked:
                return
            assert asyncio.get_running_loop().time() < deadline, (
                "後続側が先行側の行ロック待ちにならなかった"
            )
            await asyncio.sleep(0.02)


@pytest.mark.asyncio
async def test_concurrent_events_save_first_vector_and_skip_second_save(
    system_database,
    embedding_runtime,
    gated_ai_responses,
    commit_hold,
    completion_results,
):
    """先行側が保存し、後続側は行ロック解除後に生成済みを再確認して正常終了する。"""
    article = await seed_article(system_database, "https://example.com/concurrent")
    record = build_sqs_record(article)
    first_vector = [(index - 384) / 512 for index in range(768)]
    leader_ai = gated_ai_responses(first_vector)
    follower_ai = gated_ai_responses([-value for value in first_vector])

    invocations = []
    try:
        invocations.append(asyncio.create_task(invoke_sqs_record(record)))
        await leader_ai.wait_requested("先行側")
        invocations.append(asyncio.create_task(invoke_sqs_record(record)))
        await follower_ai.wait_requested("後続側")

        # 両方が未保存の記事を読んだ後、先行側だけをUPDATEまで進める。
        leader_ai.release()
        leader_pid = await commit_hold.wait_updated()

        follower_ai.release()
        await _wait_until_follower_blocked_by_row_lock(
            system_database, leader_pid, invocations
        )

        commit_hold.release()
        # 待機期限が来てもhandlerを取り消さず、finallyでゲート解除と終了待ちを行う。
        responses = await asyncio.wait_for(
            asyncio.gather(*(asyncio.shield(task) for task in invocations)),
            INVOCATION_TIMEOUT,
        )
    finally:
        # fixtureの解除はこの待機より後に走るため、先に全て開いてhandlerを終わらせる。
        leader_ai.release()
        follower_ai.release()
        commit_hold.release()
        await asyncio.wait_for(
            asyncio.gather(
                *(asyncio.shield(task) for task in invocations), return_exceptions=True
            ),
            INVOCATION_TIMEOUT,
        )

    assert responses == [{"batchItemFailures": []}, {"batchItemFailures": []}]
    assert len(completion_results) == 2
    assert completion_results.count(EmbeddingCompletion.SAVED) == 1
    assert completion_results.count(EmbeddingCompletion.ALREADY_EMBEDDED) == 1
    stored = await fetch_stored_embedding(system_database, article.analyzed_article_id)
    assert stored is not None
    assert json.loads(stored) == pytest.approx(first_vector, abs=0.001)
