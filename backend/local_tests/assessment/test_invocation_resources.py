"""製品Engineで動くAssessmentの接続返却と実接続終了を確認する。"""

import asyncio
from dataclasses import dataclass, field
from threading import Event

import httpx
import pytest
from sqlalchemy import event as sqlalchemy_event
from sqlalchemy.ext.asyncio import AsyncEngine

from app.analysis.assessment import consumer as consumer_module
from app.lambda_handlers.assessment import resources as resource_module
from local_tests.assessment.support import (
    deepseek_reply,
    fetch_stored_assessment,
    invoke_event,
    seed_curation,
    wait_for_blocked_connection,
)


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
    create_engine = resource_module.create_assessment_consumer_engine
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
        resource_module, "create_assessment_consumer_engine", create_observed_engine
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
async def test_consecutive_invocations_reuse_and_release_connections(
    system_database, assessment_runtime, pool_observer
):
    """呼び出し内では1接続を再利用し、終了時に閉じて次の記事も処理できる。"""
    for index in range(2):
        target = await seed_curation(
            system_database, f"https://example.com/resource-{index}"
        )

        response = await invoke_event(target)

        assert response == {"batchItemFailures": []}
        stored = await fetch_stored_assessment(system_database, target.curation_id)
        assert len(stored.in_scope) == 1
        pool = pool_observer.take_invocation()
        assert len(pool.connection_ids) == 1
        await _assert_pool_released(system_database, pool)


@pytest.mark.asyncio
async def test_read_session_is_returned_while_waiting_for_ai(
    system_database, assessment_runtime, pool_observer, gated_ai_responses
):
    """実HTTP応答の待機中に、読み取り接続とトランザクションが返却済みになる。"""
    target = await seed_curation(system_database, "https://example.com/ai-wait")
    gate = gated_ai_responses(deepseek_reply())
    invocation = asyncio.create_task(invoke_event(target))
    try:
        await gate.wait_requested()
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
        gate.release()
        response = await asyncio.wait_for(asyncio.shield(invocation), 15)

    assert response == {"batchItemFailures": []}
    await _assert_pool_released(system_database, pool)


@pytest.mark.asyncio
async def test_http_failure_releases_connections_and_next_invocation_succeeds(
    system_database, assessment_runtime, pool_observer, deepseek_response
):
    """SDK通信失敗の後処理でも接続を残さず、次の記事を正常に処理できる。"""
    target = await seed_curation(system_database, "https://example.com/http-error")
    deepseek_response.side_effect = httpx.ConnectError("test connection failure")

    response = await invoke_event(target)

    assert response == {
        "batchItemFailures": [{"itemIdentifier": str(target.curation_id)}]
    }
    stored = await fetch_stored_assessment(system_database, target.curation_id)
    assert [audit["event_type"] for audit in stored.audits] == ["failed"]
    await _assert_pool_released(system_database, pool_observer.take_invocation())

    deepseek_response.side_effect = None
    next_target = await seed_curation(
        system_database, "https://example.com/after-http-error"
    )
    assert await invoke_event(next_target) == {"batchItemFailures": []}
    await _assert_pool_released(system_database, pool_observer.take_invocation())


@pytest.mark.asyncio
async def test_database_failure_releases_connections(
    system_database, assessment_runtime, pool_observer, database_error_before_commit
):
    """結果・監査・Outboxの実INSERT後にDB障害が起きても、接続を返却して終了する。"""
    target = await seed_curation(
        system_database, "https://example.com/db-error-resource"
    )

    await invoke_event(target)

    assert database_error_before_commit.pending_counts == [(1, 1, 1)]
    assert len(database_error_before_commit.errors) == 1
    await _assert_pool_released(system_database, pool_observer.take_invocation())


@pytest.mark.asyncio
async def test_database_wait_timeout_releases_connections_and_allows_retry(
    system_database, assessment_runtime, pool_observer, monkeypatch
):
    """実INSERTのロック待ちで業務期限が切れても接続を解放し、同じ記事を再処理できる。"""
    target = await seed_curation(system_database, "https://example.com/db-timeout")
    timeout_ready = Event()
    deadlines = []
    create_timeout = consumer_module.timeout

    def observe_timeout(delay):
        deadline = create_timeout(delay)
        deadlines.append((asyncio.get_running_loop(), deadline))
        timeout_ready.set()
        return deadline

    with monkeypatch.context() as deadline_patch:
        deadline_patch.setattr(consumer_module, "timeout", observe_timeout)
        async with system_database.connect("vector") as blocker:
            transaction = blocker.transaction()
            await transaction.start()
            invocation = None
            try:
                await blocker.execute("LOCK TABLE analyzed_articles IN SHARE MODE")
                blocker_pid = await blocker.fetchval("SELECT pg_backend_pid()")
                invocation = asyncio.create_task(invoke_event(target))
                assert await asyncio.to_thread(timeout_ready.wait, 5), (
                    "Consumerの期限が作られなかった"
                )
                assert len(deadlines) == 1
                loop, deadline = deadlines.pop()
                blocked_pid = await wait_for_blocked_connection(
                    system_database, blocker_pid, [invocation]
                )
                pool = pool_observer.take_invocation()
                assert blocked_pid in pool.connection_ids
                assert pool.checked_out_connections
                # 実際のDB待機を確認してから、既存の業務期限だけを到来させる。
                loop.call_soon_threadsafe(deadline.reschedule, loop.time())
                response = await asyncio.wait_for(asyncio.shield(invocation), 15)
                assert deadline.expired()
                assert response == {
                    "batchItemFailures": [{"itemIdentifier": str(target.curation_id)}]
                }
                stored = await fetch_stored_assessment(
                    system_database, target.curation_id
                )
                assert (
                    not stored.in_scope
                    and not stored.out_of_scope
                    and not stored.outbox
                )
                assert [audit["event_type"] for audit in stored.audits] == ["failed"]
                await _assert_pool_released(system_database, pool)
            finally:
                try:
                    await transaction.rollback()
                finally:
                    if invocation is not None:
                        await asyncio.wait_for(asyncio.shield(invocation), 15)

    assert await invoke_event(target) == {"batchItemFailures": []}
    stored = await fetch_stored_assessment(system_database, target.curation_id)
    assert len(stored.in_scope) == 1
    await _assert_pool_released(system_database, pool_observer.take_invocation())
