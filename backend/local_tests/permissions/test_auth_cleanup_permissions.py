"""認証カウンター掃除ロールの実効権限と削除条件を確認する。"""

import asyncpg
import pytest

from local_tests.permissions.support import (
    read_column_permissions,
    read_sequence_permissions,
    read_table_permissions,
)

ROLE = "vector_auth_rate_limit_cleanup"
pytestmark = pytest.mark.asyncio


async def test_cleanup_has_only_rate_limit_delete(system_database):
    """表単位の権限を認証カウンターのDELETEだけに限定する。"""
    async with system_database.connect(ROLE) as connection:
        assert await read_table_permissions(connection) == {
            ("auth", "rateLimit", "DELETE")
        }


async def test_cleanup_can_read_only_last_request(system_database):
    """期限判定以外の認証情報を列権限でも公開しない。"""
    async with system_database.connect(ROLE) as connection:
        assert await read_column_permissions(connection) == {
            ("auth", "rateLimit", "SELECT"): {"lastRequest"}
        }


async def test_cleanup_has_no_sequence_permissions(system_database):
    """掃除ロールへ採番権限を付与しない。"""
    async with system_database.connect(ROLE) as connection:
        assert await read_sequence_permissions(connection) == set()


async def test_cleanup_delete_uses_expiry_without_reading_keys(system_database):
    """10分超だけを削除し、境界時刻と新しいカウンターを保持する。"""
    now_ms = 1_800_000_000_000
    cutoff = now_ms - 600_000
    async with system_database.connect("vector") as connection:
        await connection.executemany(
            'INSERT INTO auth."rateLimit" ("id", "key", "count", "lastRequest") '
            "VALUES (gen_random_uuid(), $1, 1, $2)",
            [("expired", cutoff - 1), ("boundary", cutoff), ("recent", now_ms)],
        )
    async with system_database.connect(ROLE) as connection:
        status = await connection.execute(
            'DELETE FROM auth."rateLimit" WHERE "lastRequest" < $1', cutoff
        )
    assert status == "DELETE 1"
    async with system_database.connect("vector") as connection:
        assert await connection.fetchval('SELECT count(*) FROM auth."rateLimit"') == 2
        assert (
            await connection.fetchval(
                'SELECT count(*) FROM auth."rateLimit" '
                "WHERE \"key\" IN ('boundary', 'recent')"
            )
            == 2
        )


@pytest.mark.parametrize(
    "statement",
    [
        'SELECT "key" FROM auth."rateLimit"',
        'SELECT "count" FROM auth."rateLimit"',
        'INSERT INTO auth."rateLimit" ("id","key","count","lastRequest") '
        "VALUES (gen_random_uuid(),'forbidden',1,0)",
        'UPDATE auth."rateLimit" SET "lastRequest"=0 WHERE false',
        'TRUNCATE auth."rateLimit"',
        'SELECT id FROM auth."user"',
        'DELETE FROM auth."session" WHERE false',
        'DELETE FROM auth."account" WHERE false',
        "SELECT id FROM public.pipeline_events",
        "DELETE FROM public.pipeline_events WHERE false",
    ],
)
async def test_cleanup_rejects_operations_outside_its_contract(
    system_database, statement
):
    """必要な列参照とDELETE以外の操作は実行時にも拒否する。"""
    async with system_database.connect(ROLE) as connection:
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await connection.execute(statement)
