"""認証ロールの許可一覧と禁止操作を確認する。"""

import asyncpg
import pytest

from local_tests.permissions.support import (
    read_column_permissions,
    read_sequence_owners,
    read_sequence_permissions,
    read_table_columns,
    read_table_permissions,
    read_tables,
)

pytestmark = pytest.mark.asyncio


async def test_auth_has_dml_only_on_auth_tables(system_database):
    """Authは認証schemaの全表にDMLだけを持つ。"""
    async with system_database.connect("vector_auth") as connection:
        tables = await read_tables(connection)
        actual = await read_table_permissions(connection)

    assert {("auth", "user"), ("auth", "account")} <= tables
    expected = {
        (schema, table, operation)
        for schema, table in tables
        if schema == "auth"
        for operation in ("SELECT", "INSERT", "UPDATE", "DELETE")
    }
    assert actual == expected


async def test_auth_has_read_insert_update_on_all_auth_columns(system_database):
    """Authの列権限は認証表の参照・追加・更新だけに限る。"""
    async with system_database.connect("vector_auth") as connection:
        columns = await read_table_columns(connection)
        actual = await read_column_permissions(connection)

    assert ("auth", "user") in columns
    expected = {
        (schema, table, operation): names
        for (schema, table), names in columns.items()
        if schema == "auth"
        for operation in ("SELECT", "INSERT", "UPDATE")
    }
    assert actual == expected


async def test_auth_has_usage_only_on_auth_sequences(system_database):
    """Authは認証schemaのsequenceに採番用USAGEだけを持つ。"""
    async with system_database.connect("vector_auth") as connection:
        sequences = await read_sequence_owners(connection)
        actual = await read_sequence_permissions(connection)

    expected = {
        (schema, sequence, "USAGE")
        for schema, sequence in sequences
        if schema == "auth"
    }
    assert actual == expected


async def test_auth_can_read_auth_user(system_database):
    """Authは認証ユーザーを参照できる。"""
    async with system_database.connect("vector_auth") as connection:
        await connection.fetch('SELECT id FROM auth."user" LIMIT 1')


async def test_auth_can_delete_rate_limit(system_database):
    """Authの保持期限処理に必要なDELETE権限を実操作で確認する。"""
    async with system_database.connect("vector_auth") as connection:
        assert (
            await connection.execute('DELETE FROM auth."rateLimit" WHERE false')
            == "DELETE 0"
        )


async def test_auth_cannot_read_watchlist(system_database):
    """Authはアプリのウォッチリストを参照できない。"""
    async with system_database.connect("vector_auth") as connection:
        with pytest.raises(asyncpg.InsufficientPrivilegeError) as failure:
            await connection.execute("SELECT * FROM public.watchlist_entries")
        assert failure.value.sqlstate == "42501"
