"""Embeddingイベントの保存結果と処理結果の整合性を確認する。"""

import asyncio
import json
from threading import Event

import httpx
import pytest

from app.analysis.embedding import consumer as consumer_module
from app.analysis.embedding.domain.value_objects import EMBEDDING_DIMENSION
from local_tests.embedding.support import invoke_event, seed_article


@pytest.mark.asyncio
async def test_event_saves_embedding_for_the_target_article(
    system_database, embedding_runtime, gemini_response
):
    """生成可能な分析済み記事2件のうち、イベントで指定した記事だけにAI応答を保存する。"""
    other_article = await seed_article(
        system_database,
        "https://example.com/other-article",
        title="イベントで指定していない記事のタイトル",
        summary="イベントで指定していない記事固有の本文",
    )
    target = await seed_article(
        system_database,
        "https://example.com/target",
        title="対象記事のタイトル",
        summary="対象記事固有の本文",
    )
    expected_vector = [(index - 384) / 512 for index in range(768)]
    sent_ai_request_bodies = []

    async def respond(request):
        sent_ai_request_bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"embeddings": [{"values": expected_vector}]})

    gemini_response.side_effect = respond
    response = await invoke_event(target)

    assert response == {"batchItemFailures": []}
    assert len(sent_ai_request_bodies) == 1
    embedding_requests = sent_ai_request_bodies[0]["requests"]
    assert len(embedding_requests) == 1
    sent_text = "\n".join(
        part["text"]
        for part in embedding_requests[0]["content"]["parts"]
        if "text" in part
    )
    assert "対象記事固有の本文" in sent_text
    assert "イベントで指定していない記事固有の本文" not in sent_text

    async with system_database.connect("vector_app") as connection:
        rows = await connection.fetch(
            "SELECT id, embedding::text AS embedding "
            "FROM analyzed_articles WHERE id=ANY($1::integer[])",
            [target.analyzed_article_id, other_article.analyzed_article_id],
        )
    stored = {row["id"]: row["embedding"] for row in rows}
    assert set(stored) == {
        target.analyzed_article_id,
        other_article.analyzed_article_id,
    }
    assert stored[target.analyzed_article_id] is not None
    assert json.loads(stored[target.analyzed_article_id]) == pytest.approx(
        expected_vector, abs=0.001
    )
    assert stored[other_article.analyzed_article_id] is None


@pytest.mark.asyncio
async def test_event_rolls_back_embedding_and_reports_failure_on_database_error(
    system_database, embedding_runtime, database_error_after_update
):
    """保存途中でDBエラーが発生したら更新がロールバックされ、イベントの処理結果も失敗になる。"""
    target = await seed_article(system_database, "https://example.com/save-failure")

    response = await invoke_event(target)

    assert database_error_after_update.updated_before_error == [
        (target.analyzed_article_id, True, True)
    ]
    assert len(database_error_after_update.database_errors) == 1
    assert response == {
        "batchItemFailures": [{"itemIdentifier": str(target.analyzed_article_id)}]
    }
    async with system_database.connect("vector_app") as connection:
        row = await connection.fetchrow(
            "SELECT embedding FROM analyzed_articles WHERE id = $1",
            target.analyzed_article_id,
        )
    assert row is not None
    assert row["embedding"] is None


@pytest.mark.asyncio
async def test_http_failure_preserves_unsaved_article_and_allows_retry(
    system_database, embedding_runtime, gemini_response
):
    """通信例外で未保存を維持し、次の記事の保存を正常に完了する。"""
    failed_article = await seed_article(
        system_database, "https://example.com/network-failure"
    )
    next_article = await seed_article(
        system_database, "https://example.com/after-network-failure"
    )
    gemini_response.side_effect = httpx.ConnectError("test connection failure")

    response = await invoke_event(failed_article)
    gemini_response.assert_awaited()
    assert response == {
        "batchItemFailures": [
            {"itemIdentifier": str(failed_article.analyzed_article_id)}
        ]
    }
    async with system_database.connect("vector_app") as connection:
        stored = await connection.fetchrow(
            "SELECT embedding::text AS embedding FROM analyzed_articles WHERE id=$1",
            failed_article.analyzed_article_id,
        )
        assert stored is not None
        assert stored["embedding"] is None

    gemini_response.side_effect = None
    response = await invoke_event(next_article)
    assert response == {"batchItemFailures": []}
    async with system_database.connect("vector_app") as connection:
        stored = await connection.fetchval(
            "SELECT embedding::text FROM analyzed_articles WHERE id=$1",
            next_article.analyzed_article_id,
        )
        assert stored is not None
        assert json.loads(stored) == pytest.approx(
            [0.2] * EMBEDDING_DIMENSION, abs=0.001
        )


async def _wait_for_blocked_connection(connection, blocker_pid, invocation):
    deadline = asyncio.get_running_loop().time() + 3.0
    while True:
        assert not invocation.done(), "DBのロック待ちになる前に処理が終了した"
        blocked_pid = await connection.fetchval(
            "SELECT pid FROM pg_stat_activity "
            "WHERE datname=current_database() "
            "AND application_name='vector-embedding-consumer' "
            "AND wait_event_type='Lock' "
            "AND $1::integer=ANY(pg_blocking_pids(pid))",
            blocker_pid,
        )
        if blocked_pid is not None:
            return blocked_pid
        assert asyncio.get_running_loop().time() < deadline, (
            "保存処理がDBのロック待ちにならなかった"
        )
        await asyncio.sleep(0.02)


@pytest.mark.asyncio
async def test_database_wait_timeout_preserves_unsaved_article_and_allows_retry(
    system_database, embedding_runtime, monkeypatch
):
    """DB操作のタイムアウトでは未保存を維持し、同じ記事を再処理できる。"""
    article = await seed_article(system_database, "https://example.com/db-timeout")
    timeout_ready = Event()
    deadlines = []
    create_timeout = consumer_module.timeout

    def observe_timeout(delay):
        loop = asyncio.get_running_loop()
        timeout = create_timeout(delay)
        deadlines.append((loop, timeout))
        timeout_ready.set()
        return timeout

    with monkeypatch.context() as deadline_patch:
        deadline_patch.setattr(consumer_module, "timeout", observe_timeout)
        async with system_database.connect("vector_app") as blocker:
            transaction = blocker.transaction()
            await transaction.start()
            invocation = None
            try:
                assert (
                    await blocker.fetchval(
                        "SELECT id FROM analyzed_articles WHERE id=$1 FOR UPDATE",
                        article.analyzed_article_id,
                    )
                    == article.analyzed_article_id
                )
                blocker_pid = await blocker.fetchval("SELECT pg_backend_pid()")
                invocation = asyncio.create_task(invoke_event(article))
                assert await asyncio.to_thread(timeout_ready.wait, 5), (
                    "Consumerの処理期限が作られなかった"
                )
                assert len(deadlines) == 1
                loop, timeout = deadlines.pop()
                async with system_database.connect("vector_app") as monitor:
                    await _wait_for_blocked_connection(monitor, blocker_pid, invocation)

                # 実際のロック待ちを確認してから、元のtimeoutの期限だけを到来させる。
                loop.call_soon_threadsafe(timeout.reschedule, loop.time())
                response = await asyncio.wait_for(asyncio.shield(invocation), 15)
                assert timeout.expired()
                assert response == {
                    "batchItemFailures": [
                        {"itemIdentifier": str(article.analyzed_article_id)}
                    ]
                }
                async with system_database.connect("vector_app") as connection:
                    stored = await connection.fetchrow(
                        "SELECT embedding::text AS embedding "
                        "FROM analyzed_articles WHERE id=$1",
                        article.analyzed_article_id,
                    )
                    assert stored is not None
                    assert stored["embedding"] is None
            finally:
                try:
                    await transaction.rollback()
                finally:
                    if invocation is not None:
                        await asyncio.wait_for(asyncio.shield(invocation), 15)

    response = await invoke_event(article)
    assert response == {"batchItemFailures": []}
    async with system_database.connect("vector_app") as connection:
        stored = await connection.fetchval(
            "SELECT embedding::text FROM analyzed_articles WHERE id=$1",
            article.analyzed_article_id,
        )
        assert stored is not None
        assert json.loads(stored) == pytest.approx(
            [0.2] * EMBEDDING_DIMENSION, abs=0.001
        )
