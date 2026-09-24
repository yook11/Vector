"""共通入口を直接呼び、実DB接続の生存期間と失敗時の解放を確認する。"""

import asyncio
from unittest.mock import Mock

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.db.errors import DatabaseError
from app.lambda_handlers.backfill import resources
from local_tests.backfill.support import assert_connections_closed


@pytest.mark.asyncio
async def test_connections_live_until_scope_exit(system_database, lifecycle):
    """借用中は接続を維持し、スコープ終了後は実DBから消える。"""
    async with resources.open_backfill_resources(
        lifecycle.settings, stage="curation", route=lifecycle.route
    ) as opened:
        async with opened.session_factory() as session:
            pid = await session.scalar(text("SELECT pg_backend_pid()"))
        scope = lifecycle.invocations[-1]
        assert pid in scope.pids
        assert not scope.disposed
        async with system_database.connect("vector_backfill") as connection:
            assert (
                await connection.fetchval(
                    "SELECT count(*) FROM pg_stat_activity WHERE pid=$1", pid
                )
                == 1
            )
    await assert_connections_closed(system_database, scope)


@pytest.mark.asyncio
async def test_sessions_reuse_one_connection_within_invocation(lifecycle):
    """同じ呼び出し内のセッションは一つの実接続を再利用する。"""
    async with resources.open_backfill_resources(
        lifecycle.settings, stage="curation", route=lifecycle.route
    ) as opened:
        async with opened.session_factory() as session:
            first = await session.scalar(text("SELECT pg_backend_pid()"))
        async with opened.session_factory() as session:
            second = await session.scalar(text("SELECT pg_backend_pid()"))
        assert first == second


@pytest.mark.asyncio
async def test_invocations_do_not_share_connections_or_loops(
    system_database, lifecycle
):
    """別の同期起動は接続とイベントループを持ち越さない。"""

    async def borrow():
        async with resources.open_backfill_resources(
            lifecycle.settings, stage="curation", route=lifecycle.route
        ) as opened:
            async with opened.session_factory() as session:
                await session.execute(text("SELECT 1"))

    await asyncio.to_thread(asyncio.run, borrow())
    await asyncio.to_thread(asyncio.run, borrow())
    first, second = lifecycle.invocations
    assert first.pids and second.pids
    assert first.pids.isdisjoint(second.pids)
    assert first.loop is not second.loop
    assert first.loop.is_closed() and second.loop.is_closed()
    await assert_connections_closed(system_database, first)
    await assert_connections_closed(system_database, second)


@pytest.mark.asyncio
async def test_borrower_failure_releases_connections(system_database, lifecycle):
    """実接続使用後の業務例外を保持して全資源を解放する。"""
    error = RuntimeError("borrower")
    with pytest.raises(RuntimeError) as caught:
        async with resources.open_backfill_resources(
            lifecycle.settings, stage="curation", route=lifecycle.route
        ) as opened:
            async with opened.session_factory() as session:
                await session.execute(text("SELECT 1"))
            raise error
    assert caught.value is error
    await assert_connections_closed(system_database, lifecycle.invocations[-1])


@pytest.mark.asyncio
async def test_sql_failure_releases_connections(system_database, lifecycle):
    """実SQL障害も正常終了に変換せず接続を解放する。"""
    with pytest.raises(DatabaseError):
        async with resources.open_backfill_resources(
            lifecycle.settings, stage="curation", route=lifecycle.route
        ) as opened:
            async with opened.session_factory() as session:
                await session.execute(text("SELECT 1 / 0"))
    await assert_connections_closed(system_database, lifecycle.invocations[-1])


@pytest.mark.asyncio
async def test_external_cancellation_releases_connections(system_database, lifecycle):
    """貸出中の実セッションを外部キャンセルしてもDB接続が残らない。"""
    entered = asyncio.Event()

    async def borrow():
        async with resources.open_backfill_resources(
            lifecycle.settings, stage="curation", route=lifecycle.route
        ) as opened:
            async with opened.session_factory() as session:
                await session.execute(text("SELECT 1"))
                entered.set()
                await asyncio.Event().wait()

    task = asyncio.create_task(borrow())
    try:
        await asyncio.wait_for(entered.wait(), 5)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    await assert_connections_closed(system_database, lifecycle.invocations[-1])


@pytest.mark.asyncio
async def test_rds_initialization_failure_does_not_create_engine(
    lifecycle, monkeypatch
):
    """RDS初期化失敗では後続資源を生成せず例外を伝える。"""
    error = RuntimeError("rds")
    monkeypatch.setattr(resources, "Session", Mock(side_effect=error))
    with pytest.raises(RuntimeError) as caught:
        async with resources.open_backfill_resources(
            lifecycle.settings, stage="curation", route=lifecycle.route
        ):
            pytest.fail("must not yield")
    assert caught.value is error
    assert lifecycle.invocations == []


@pytest.mark.asyncio
async def test_engine_initialization_failure_closes_rds(lifecycle, monkeypatch):
    """engine生成が失敗した場合も取得済みRDSクライアントを閉じる。"""
    error = RuntimeError("engine")
    monkeypatch.setattr(resources, "create_backfill_engine", Mock(side_effect=error))
    with pytest.raises(RuntimeError) as caught:
        async with resources.open_backfill_resources(
            lifecycle.settings, stage="curation", route=lifecycle.route
        ):
            pytest.fail("must not yield")
    assert caught.value is error
    assert lifecycle.invocations[-1].order == ["rds"]


@pytest.mark.asyncio
async def test_publisher_initialization_failure_disposes_engine(
    system_database, lifecycle, monkeypatch
):
    """publisher生成失敗では取得済みの実engineとRDSを逆順に解放する。"""
    error = RuntimeError("publisher")
    monkeypatch.setattr(resources.SqsSender, "from_session", Mock(side_effect=error))
    with pytest.raises(RuntimeError) as caught:
        async with resources.open_backfill_resources(
            lifecycle.settings, stage="curation", route=lifecycle.route
        ):
            pytest.fail("must not yield")
    assert caught.value is error
    await assert_connections_closed(system_database, lifecycle.invocations[-1])


@pytest.fixture
def failing_dispose(monkeypatch, lifecycle):
    dispose = AsyncEngine.dispose
    error = RuntimeError("dispose")

    async def close_then_fail(engine, *args, **kwargs):
        await dispose(engine, *args, **kwargs)
        raise error

    monkeypatch.setattr(AsyncEngine, "dispose", close_then_fail)
    return error


@pytest.mark.asyncio
async def test_disposal_only_failure_is_propagated(
    system_database, lifecycle, failing_dispose
):
    """実dispose後の単独終了失敗を正常扱いせず、RDSも解放する。"""
    with pytest.raises(RuntimeError) as caught:
        async with resources.open_backfill_resources(
            lifecycle.settings, stage="curation", route=lifecycle.route
        ) as opened:
            async with opened.session_factory() as session:
                await session.execute(text("SELECT 1"))
    assert caught.value is failing_dispose
    await assert_connections_closed(system_database, lifecycle.invocations[-1])


@pytest.mark.asyncio
async def test_disposal_failure_preserves_borrower_error(
    system_database, lifecycle, failing_dispose
):
    """終了処理の失敗が先行する業務例外を上書きしない。"""
    error = ValueError("borrower")
    with pytest.raises(ValueError) as caught:
        async with resources.open_backfill_resources(
            lifecycle.settings, stage="curation", route=lifecycle.route
        ) as opened:
            async with opened.session_factory() as session:
                await session.execute(text("SELECT 1"))
            raise error
    assert caught.value is error
    await assert_connections_closed(system_database, lifecycle.invocations[-1])


@pytest.mark.asyncio
async def test_disposal_failure_preserves_cancellation(
    system_database, lifecycle, failing_dispose
):
    """先行するキャンセルは終了処理が失敗しても保持される。"""
    error = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError) as caught:
        async with resources.open_backfill_resources(
            lifecycle.settings, stage="curation", route=lifecycle.route
        ) as opened:
            async with opened.session_factory() as session:
                await session.execute(text("SELECT 1"))
            raise error
    assert caught.value is error
    await assert_connections_closed(system_database, lifecycle.invocations[-1])


@pytest.mark.asyncio
async def test_rds_close_failure_is_propagated(system_database, lifecycle):
    """engineを解放した後のRDS終了失敗を正常扱いしない。"""
    error = RuntimeError("rds_close")
    with pytest.raises(RuntimeError) as caught:
        async with resources.open_backfill_resources(
            lifecycle.settings, stage="curation", route=lifecycle.route
        ) as opened:
            async with opened.session_factory() as session:
                await session.execute(text("SELECT 1"))
            rds = lifecycle.invocations[-1].rds
            close = rds.close

            def close_then_fail():
                close()
                raise error

            rds.close = close_then_fail
    assert caught.value is error
    await assert_connections_closed(system_database, lifecycle.invocations[-1])


@pytest.mark.asyncio
async def test_cleanup_cancellation_propagates_after_releasing_rds(
    system_database, lifecycle, monkeypatch
):
    """終了中のキャンセルは残りのRDSを解放してから伝える。"""
    dispose = AsyncEngine.dispose
    cancelled = asyncio.CancelledError()

    async def close_then_cancel(engine, *args, **kwargs):
        await dispose(engine, *args, **kwargs)
        raise cancelled

    monkeypatch.setattr(AsyncEngine, "dispose", close_then_cancel)
    with pytest.raises(asyncio.CancelledError) as caught:
        async with resources.open_backfill_resources(
            lifecycle.settings, stage="curation", route=lifecycle.route
        ) as opened:
            async with opened.session_factory() as session:
                await session.execute(text("SELECT 1"))
    assert caught.value is cancelled
    await assert_connections_closed(system_database, lifecycle.invocations[-1])
