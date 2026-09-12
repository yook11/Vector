"""Embedding呼び出しが保存を確定してDB接続を解放することを確認する。"""

import asyncio
import json
from dataclasses import dataclass, field
from threading import Event

import httpx
import pytest
from sqlalchemy import event as sqlalchemy_event
from sqlalchemy.ext.asyncio import AsyncEngine

from app.analysis.embedding import consumer as consumer_module
from app.analysis.embedding.domain.value_objects import EMBEDDING_DIMENSION
from app.lambda_handlers.embedding import resources as resource_module
from local_tests.embedding.support import invoke_event, seed_article


@dataclass
class InvocationPool:
    connection_ids: set[int] = field(default_factory=set)
    checked_out_connections: set[int] = field(default_factory=set)
    checked_out_before_dispose: int | None = None
    dispose_completed: bool = False


class PoolObserver:
    def __init__(self):
        self.pending: list[InvocationPool] = []

    def take_invocation(self) -> InvocationPool:
        assert len(self.pending) == 1, "1回の呼び出しで作られるEngineは1つ"
        return self.pending.pop()


@pytest.fixture
def pool_observer(monkeypatch):
    observer = PoolObserver()
    create_engine = resource_module.create_embedding_consumer_engine
    pools = {}
    dispose = AsyncEngine.dispose

    async def observed_dispose(engine, *args, **kwargs):
        observation = pools.get(engine)
        if observation is not None:
            observation.checked_out_before_dispose = engine.pool.checkedout()
        await dispose(engine, *args, **kwargs)
        if observation is not None:
            observation.dispose_completed = True

    monkeypatch.setattr(AsyncEngine, "dispose", observed_dispose)

    def create_observed_engine(*args, **kwargs):
        engine = create_engine(*args, **kwargs)
        observation = InvocationPool()
        observer.pending.append(observation)
        pools[engine] = observation

        @sqlalchemy_event.listens_for(engine.sync_engine, "connect")
        def record_connection(connection, _):
            cursor = connection.cursor()
            try:
                cursor.execute("SELECT pg_backend_pid()")
                observation.connection_ids.add(cursor.fetchone()[0])
            finally:
                cursor.close()

        @sqlalchemy_event.listens_for(engine.sync_engine, "checkout")
        def record_checkout(connection, record, proxy):
            observation.checked_out_connections.add(id(record))

        @sqlalchemy_event.listens_for(engine.sync_engine, "checkin")
        def record_checkin(connection, record):
            observation.checked_out_connections.discard(id(record))

        return engine

    monkeypatch.setattr(
        resource_module, "create_embedding_consumer_engine", create_observed_engine
    )
    return observer


async def _assert_connections_closed(connection, connection_ids):
    deadline = asyncio.get_running_loop().time() + 2.0
    while True:
        remaining = await connection.fetchval(
            "SELECT count(*) FROM pg_stat_activity "
            "WHERE datname=current_database() AND pid=ANY($1::integer[])",
            list(connection_ids),
        )
        if remaining == 0:
            return
        assert asyncio.get_running_loop().time() < deadline, (
            f"ハンドラー終了後もDB接続が残っている: {connection_ids}"
        )
        await asyncio.sleep(0.02)


async def _assert_pool_released(database, pool):
    assert pool.connection_ids
    assert not pool.checked_out_connections
    assert pool.checked_out_before_dispose == 0
    assert pool.dispose_completed
    async with database.connect("vector_app") as connection:
        await _assert_connections_closed(connection, pool.connection_ids)


@pytest.mark.asyncio
async def test_handler_releases_connections_before_returning_and_can_run_again(
    system_database, embedding_runtime, pool_observer
):
    """異なる記事の連続呼び出しで、保存確定と貸出返却・実接続終了を確認する。"""
    first_article = await seed_article(system_database, "https://example.com/first")
    second_article = await seed_article(system_database, "https://example.com/second")

    response = await invoke_event(first_article)
    first_pool = pool_observer.take_invocation()
    assert response == {"batchItemFailures": []}
    async with system_database.connect("vector_app") as connection:
        stored = await connection.fetchval(
            "SELECT embedding::text FROM analyzed_articles WHERE id=$1",
            first_article.analyzed_article_id,
        )
        assert stored is not None
        assert json.loads(stored) == pytest.approx(
            [0.2] * EMBEDDING_DIMENSION, abs=0.001
        )
    await _assert_pool_released(system_database, first_pool)

    response = await invoke_event(second_article)
    second_pool = pool_observer.take_invocation()
    assert response == {"batchItemFailures": []}
    async with system_database.connect("vector_app") as connection:
        stored = await connection.fetchval(
            "SELECT embedding::text FROM analyzed_articles WHERE id=$1",
            second_article.analyzed_article_id,
        )
        assert stored is not None
        assert json.loads(stored) == pytest.approx(
            [0.2] * EMBEDDING_DIMENSION, abs=0.001
        )
    await _assert_pool_released(system_database, second_pool)


@pytest.mark.asyncio
async def test_handler_returns_read_connection_while_waiting_for_ai(
    system_database, embedding_runtime, pool_observer, gemini_response
):
    """AI応答を停止した実処理で接続返却とトランザクション終了を確認する。"""
    article = await seed_article(system_database, "https://example.com/ai-wait")
    request_arrived = Event()
    release_response = Event()
    success_response = gemini_response.return_value

    async def pause_response(request):
        request_arrived.set()
        if not await asyncio.to_thread(release_response.wait, 10):
            raise TimeoutError("AI応答の再開指示が届かなかった")
        return success_response

    gemini_response.side_effect = pause_response
    invocation = asyncio.create_task(invoke_event(article))
    try:
        assert await asyncio.to_thread(request_arrived.wait, 5), (
            "AIリクエストが到達しなかった"
        )
        pool = pool_observer.take_invocation()
        assert pool.connection_ids
        assert not pool.checked_out_connections
        assert not pool.dispose_completed
        assert not invocation.done()

        async with system_database.connect("vector_app") as connection:
            rows = await connection.fetch(
                "SELECT pid, state, xact_start FROM pg_stat_activity "
                "WHERE datname=current_database() AND pid=ANY($1::integer[])",
                list(pool.connection_ids),
            )
            assert {row["pid"] for row in rows} == pool.connection_ids
            assert all(row["state"] == "idle" for row in rows)
            assert all(row["xact_start"] is None for row in rows)
    finally:
        release_response.set()
        response = await asyncio.wait_for(invocation, timeout=15)

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
    await _assert_pool_released(system_database, pool)


@pytest.mark.asyncio
async def test_handler_releases_connections_after_ai_network_failure(
    system_database, embedding_runtime, pool_observer, gemini_response
):
    """通信例外の後処理で接続を残さず、次の呼び出しを正常に完了する。"""
    failed_article = await seed_article(
        system_database, "https://example.com/network-failure"
    )
    next_article = await seed_article(
        system_database, "https://example.com/after-network-failure"
    )
    gemini_response.side_effect = httpx.ConnectError("test connection failure")

    response = await invoke_event(failed_article)
    failed_pool = pool_observer.take_invocation()
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
    await _assert_pool_released(system_database, failed_pool)

    gemini_response.side_effect = None
    response = await invoke_event(next_article)
    next_pool = pool_observer.take_invocation()
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
    await _assert_pool_released(system_database, next_pool)


@pytest.mark.asyncio
async def test_handler_releases_connections_after_database_error_after_update(
    system_database, embedding_runtime, pool_observer, database_error_after_update
):
    """更新後にDBエラーが発生しても、接続がプールへ返却され、処理終了後に実接続が残らない。"""
    article = await seed_article(
        system_database, "https://example.com/db-error-after-update"
    )

    await invoke_event(article)
    pool = pool_observer.take_invocation()

    assert database_error_after_update.updated_before_error == [
        (article.analyzed_article_id, True, True)
    ]
    assert len(database_error_after_update.database_errors) == 1
    await _assert_pool_released(system_database, pool)


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
async def test_handler_releases_connections_after_database_operation_timeout(
    system_database, embedding_runtime, pool_observer, monkeypatch
):
    """DB操作のタイムアウト後に接続を解放し、次の記事を正常に処理できる。"""
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
                    blocked_pid = await _wait_for_blocked_connection(
                        monitor, blocker_pid, invocation
                    )

                pool = pool_observer.take_invocation()
                assert blocked_pid in pool.connection_ids
                assert pool.checked_out_connections
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
                await _assert_pool_released(system_database, pool)
            finally:
                try:
                    await transaction.rollback()
                finally:
                    if invocation is not None:
                        await asyncio.wait_for(asyncio.shield(invocation), 15)

    response = await invoke_event(article)
    next_pool = pool_observer.take_invocation()
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
    await _assert_pool_released(system_database, next_pool)
