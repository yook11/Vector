"""調査ロールがpublicの全表を読めて、書き込みとauthの参照を持たないことを確認する。"""

import asyncpg
import pytest

from local_tests.permissions.support import (
    read_column_permissions,
    read_sequence_permissions,
    read_table_columns,
    read_table_permissions,
    read_tables,
)

ROLE = "vector_investigation"
pytestmark = pytest.mark.asyncio


async def test_investigation_can_select_every_public_table_only(system_database):
    """表単位の権限はpublicの全表のSELECTだけで、authの表には何も持たない。"""
    async with system_database.connect(ROLE) as connection:
        tables = await read_tables(connection)
        actual = await read_table_permissions(connection)

    assert {("public", "categories"), ("auth", "account")} <= tables
    expected = {
        (schema, table, "SELECT") for schema, table in tables if schema == "public"
    }
    assert actual == expected


async def test_investigation_can_select_every_public_column_only(system_database):
    """列単位の権限はpublicの全列のSELECTだけに限る。"""
    async with system_database.connect(ROLE) as connection:
        columns = await read_table_columns(connection)
        actual = await read_column_permissions(connection)

    expected = {
        (schema, table, "SELECT"): names
        for (schema, table), names in columns.items()
        if schema == "public"
    }
    assert actual == expected


async def test_investigation_has_no_sequence_permissions(system_database):
    """書き込みをしないため、採番の権限を持たない。"""
    async with system_database.connect(ROLE) as connection:
        assert await read_sequence_permissions(connection) == set()


async def test_investigation_can_read_public_rows(system_database):
    """publicの表の行を読める。"""
    async with system_database.connect(ROLE) as connection:
        assert await connection.fetchval("SELECT count(*) FROM categories")


@pytest.mark.parametrize(
    "statement",
    [
        "INSERT INTO categories (slug, name) VALUES ('investigation', 'forbidden')",
        "UPDATE categories SET name=name WHERE false",
        "DELETE FROM categories WHERE false",
        "TRUNCATE categories",
    ],
)
async def test_investigation_cannot_write_public_table(system_database, statement):
    """publicの表の追加・更新・削除・全削除を拒否する。"""
    async with system_database.connect(ROLE) as connection:
        with pytest.raises(asyncpg.InsufficientPrivilegeError) as failure:
            await connection.execute(statement)
        assert failure.value.sqlstate == "42501"


@pytest.mark.parametrize(
    "statement",
    [
        'SELECT * FROM auth."user" LIMIT 1',
        "SELECT * FROM auth.account LIMIT 1",
        "SELECT * FROM auth.session LIMIT 1",
        "SELECT * FROM auth.verification LIMIT 1",
    ],
)
async def test_investigation_cannot_read_auth_tables(system_database, statement):
    """利用者の情報とセッション・パスワード・OAuthのトークンを読めない。"""
    async with system_database.connect(ROLE) as connection:
        with pytest.raises(asyncpg.InsufficientPrivilegeError) as failure:
            await connection.fetch(statement)
        assert failure.value.sqlstate == "42501"
