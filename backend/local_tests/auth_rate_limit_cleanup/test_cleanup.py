"""実Lambda入口から期限付きDELETEと失敗時の振る舞いを確認する。"""

import asyncio
import json

import pytest

from app.lambda_handlers.auth_rate_limit_cleanup.handler import (
    AuthRateLimitCleanupFailed,
)

pytestmark = pytest.mark.asyncio


def completed_logs(capsys):
    return [
        row
        for line in capsys.readouterr().out.splitlines()
        if (row := json.loads(line)).get("event") == "auth_rate_limit_cleanup_completed"
    ]


async def remaining(database):
    async with database.connect("vector") as connection:
        return await connection.fetchval('SELECT count(*) FROM auth."rateLimit"')


async def test_deletes_only_expired_counters(cleanup, counters):
    """専用ロールの実処理で10分境界と新しい行を維持する。"""
    await cleanup.invoke()
    async with counters.connect("vector") as connection:
        rows = await connection.fetch(
            'SELECT "key" FROM auth."rateLimit" ORDER BY "key"'
        )
    assert [r["key"] for r in rows] == ["boundary", "recent"]


async def test_reports_committed_row_count(cleanup, counters, capsys):
    """成功件数はcommitされたDELETEの結果を表す。"""
    await cleanup.invoke()
    assert completed_logs(capsys)[0]["deleted"] == 1


async def test_empty_table_is_successful(cleanup, capsys):
    """対象0件でも正常に完了する。"""
    await cleanup.invoke()
    assert completed_logs(capsys)[0]["deleted"] == 0


async def test_repeated_invocation_does_not_delete_newer_rows(
    cleanup, counters, capsys
):
    """再実行では削除対象がなくなり新しい行が残る。"""
    await cleanup.invoke()
    await cleanup.invoke()
    assert [log["deleted"] for log in completed_logs(capsys)] == [1, 0]
    assert await remaining(counters) == 2


async def test_overlapping_invocations_are_safe_without_lock(cleanup, counters, capsys):
    """重複呼び出しでも削除件数が二重計上されず境界以降が残る。"""
    await asyncio.gather(cleanup.invoke(), cleanup.invoke())
    assert sorted(log["deleted"] for log in completed_logs(capsys)) == [0, 1]
    assert await remaining(counters) == 2


async def test_event_cannot_override_retention(cleanup, counters):
    """イベントの値で削除条件を広げることはできない。"""
    await cleanup.invoke({"cutoff_ms": 9_999_999_999_999, "retention_minutes": 0})
    assert await remaining(counters) == 2


async def test_sql_failure_rolls_back_delete(cleanup, counters):
    """DELETE直後の失敗では実トランザクションがrollbackされる。"""

    def fail_after_delete(conn, cursor, statement, parameters, context, executemany):
        if statement.startswith("DELETE"):
            raise RuntimeError("private database detail")

    cleanup.listeners.append(("after_cursor_execute", fail_after_delete))
    with pytest.raises(AuthRateLimitCleanupFailed):
        await cleanup.invoke()
    assert await remaining(counters) == 3


async def test_commit_failure_has_no_success_log(cleanup, counters, capsys):
    """commit失敗を成功として記録しない。"""

    def fail_commit(connection):
        raise RuntimeError("private commit detail")

    cleanup.listeners.append(("commit", fail_commit))
    with pytest.raises(AuthRateLimitCleanupFailed):
        await cleanup.invoke()
    assert completed_logs(capsys) == []


async def test_connect_failure_is_sanitized(cleanup, monkeypatch, capsys):
    """接続失敗の秘密がログとLambda例外へ出ない。"""

    def fail_signer(*args, **kwargs):
        async def password():
            raise RuntimeError("secret-token-and-database-url")

        return password

    monkeypatch.setattr(cleanup.module, "build_iam_password_provider", fail_signer)
    with pytest.raises(AuthRateLimitCleanupFailed) as failure:
        await cleanup.invoke()
    output = capsys.readouterr().out
    assert '"phase": "connect"' in output
    assert "secret-token-and-database-url" not in output + str(failure.value)
    assert failure.value.__suppress_context__


async def test_success_releases_engine_and_signer(cleanup):
    """正常終了で呼び出し専用の資源を解放する。"""
    await cleanup.invoke()
    assert len(cleanup.disposed) == 1
    cleanup.clients[0].close.assert_called_once()


async def test_failure_releases_engine_and_signer(cleanup, counters):
    """SQL失敗後も呼び出し専用の資源を解放する。"""

    def fail_delete(conn, cursor, statement, parameters, context, executemany):
        if statement.startswith("DELETE"):
            raise RuntimeError("failed")

    cleanup.listeners.append(("before_cursor_execute", fail_delete))
    with pytest.raises(AuthRateLimitCleanupFailed):
        await cleanup.invoke()
    assert len(cleanup.disposed) == 1
    cleanup.clients[0].close.assert_called_once()


async def test_database_wait_limits_are_set(cleanup):
    """実接続にSQLとロック待ちの上限が適用される。"""
    observed = []

    def observe(conn, cursor, statement, parameters, context, executemany):
        if statement.startswith("DELETE"):
            observed.append(
                (
                    conn.exec_driver_sql("SHOW statement_timeout").scalar(),
                    conn.exec_driver_sql("SHOW lock_timeout").scalar(),
                )
            )

    cleanup.listeners.append(("before_cursor_execute", observe))
    await cleanup.invoke()
    assert observed == [("15s", "3s")]


async def test_commit_failure_rolls_back_delete(cleanup, counters):
    """commit直前の失敗では削除が確定しない。"""

    def fail_commit(connection):
        raise RuntimeError("commit failed")

    cleanup.listeners.append(("commit", fail_commit))
    with pytest.raises(AuthRateLimitCleanupFailed):
        await cleanup.invoke()
    assert await remaining(counters) == 3


async def test_runs_one_delete_even_when_many_rows_expire(cleanup, counters):
    """対象件数にかかわらずDELETEは一度だけ実行する。"""
    statements = []

    def observe(conn, cursor, statement, parameters, context, executemany):
        if statement.startswith("DELETE"):
            statements.append(statement)

    cleanup.listeners.append(("before_cursor_execute", observe))
    async with counters.connect("vector") as connection:
        await connection.execute(
            'INSERT INTO auth."rateLimit" ("id", "key", "count", "lastRequest") '
            "SELECT gen_random_uuid(), 'old-' || n, 1, 0 "
            "FROM generate_series(1, 2000) n"
        )
    await cleanup.invoke()
    assert len(statements) == 1
    assert await remaining(counters) == 2
