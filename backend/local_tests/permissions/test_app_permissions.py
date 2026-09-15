"""Appロールの既存権限と認証データへのアクセス境界を確認する。"""

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


async def test_app_has_public_dml_and_auth_user_read_reference(system_database):
    """Appの表権限はpublicのDMLと認証ユーザーの参照に限る。"""
    async with system_database.connect("vector_app") as connection:
        tables = await read_tables(connection)
        actual = await read_table_permissions(connection)

    assert {("public", "categories"), ("auth", "user")} <= tables
    expected = {
        (schema, table, operation)
        for schema, table in tables
        if schema == "public"
        for operation in ("SELECT", "INSERT", "UPDATE", "DELETE")
    } | {("auth", "user", "SELECT"), ("auth", "user", "REFERENCES")}
    assert actual == expected


async def test_app_has_public_columns_and_auth_user_read_reference(system_database):
    """Appの列権限はpublicの参照・追加・更新と認証ユーザーの参照に限る。"""
    async with system_database.connect("vector_app") as connection:
        columns = await read_table_columns(connection)
        actual = await read_column_permissions(connection)

    expected = {
        (schema, table, operation): names
        for (schema, table), names in columns.items()
        if schema == "public"
        for operation in ("SELECT", "INSERT", "UPDATE")
    }
    expected[("auth", "user", "SELECT")] = columns[("auth", "user")]
    expected[("auth", "user", "REFERENCES")] = columns[("auth", "user")]
    assert actual == expected


async def test_app_sequence_permissions_preserve_existing_read_write_grants(
    system_database,
):
    """Appの採番権限とy2が付与した既存sequenceの追加権限を維持する。"""
    read_write_tables = {
        "agent_message_sources",
        "analyzable_articles",
        "analyzed_articles",
        "article_curations",
        "curation_noises",
        "incomplete_articles",
        "categories",
        "news_sources",
        "out_of_scope_articles",
        "pipeline_events",
        "query_embedding_cache",
        "weekly_briefings",
    }
    async with system_database.connect("vector_app") as connection:
        sequences = await read_sequence_owners(connection)
        actual = await read_sequence_permissions(connection)

    expected = {
        (schema, sequence, "USAGE")
        for schema, sequence in sequences
        if schema == "public"
    } | {
        (schema, sequence, operation)
        for (schema, sequence), table in sequences.items()
        if schema == "public" and table in read_write_tables
        for operation in ("SELECT", "UPDATE")
    }
    assert actual == expected


async def test_app_can_read_auth_user(system_database):
    """Appは必要な認証ユーザーを参照できる。"""
    async with system_database.connect("vector_app") as connection:
        await connection.fetch('SELECT id FROM auth."user" LIMIT 1')


async def test_app_can_commit_category_crud(system_database):
    """自分で追加したカテゴリの読取・更新・削除を別接続から確認する。"""
    async with system_database.connect("vector_app") as connection:
        category_id = await connection.fetchval(
            "INSERT INTO categories (slug,name) VALUES ('permissions','before') "
            "RETURNING id"
        )
    async with system_database.connect("vector_app") as connection:
        assert (
            await connection.fetchval(
                "SELECT name FROM categories WHERE id=$1", category_id
            )
            == "before"
        )
        assert (
            await connection.execute(
                "UPDATE categories SET name='after' WHERE id=$1", category_id
            )
            == "UPDATE 1"
        )
    async with system_database.connect("vector_app") as connection:
        assert (
            await connection.fetchval(
                "SELECT name FROM categories WHERE id=$1", category_id
            )
            == "after"
        )
        assert (
            await connection.execute("DELETE FROM categories WHERE id=$1", category_id)
            == "DELETE 1"
        )
    async with system_database.connect("vector_app") as connection:
        assert not await connection.fetchval(
            "SELECT EXISTS (SELECT 1 FROM categories WHERE id=$1)", category_id
        )


async def test_app_cannot_create_public_table(system_database):
    """Appにpublic内のDDLを許可しない。"""
    async with system_database.connect("vector_app") as connection:
        with pytest.raises(asyncpg.InsufficientPrivilegeError) as failure:
            await connection.execute(
                "CREATE TABLE public.permission_forbidden (id integer)"
            )
        assert failure.value.sqlstate == "42501"


async def test_app_cannot_read_auth_account(system_database):
    """Appは認証アカウントを参照できない。"""
    async with system_database.connect("vector_app") as connection:
        with pytest.raises(asyncpg.InsufficientPrivilegeError) as failure:
            await connection.execute("SELECT * FROM auth.account")
        assert failure.value.sqlstate == "42501"


async def test_app_cannot_insert_auth_user(system_database):
    """Appに認証ユーザーの作成を許可しない。"""
    async with system_database.connect("vector_app") as connection:
        with pytest.raises(asyncpg.InsufficientPrivilegeError) as failure:
            await connection.execute(
                'INSERT INTO auth."user" (id, name, email, '
                '"emailVerified", "createdAt", "updatedAt", role) '
                "VALUES (gen_random_uuid(), 'test', 'permission@example.com', "
                "false, now(), now(), 'user')"
            )
        assert failure.value.sqlstate == "42501"


async def test_app_cannot_change_auth_user_role(system_database):
    """Appに認証ユーザーの権限変更を許可しない。"""
    async with system_database.connect("vector_app") as connection:
        with pytest.raises(asyncpg.InsufficientPrivilegeError) as failure:
            await connection.execute('UPDATE auth."user" SET role=role WHERE false')
        assert failure.value.sqlstate == "42501"


async def test_app_cannot_delete_auth_user(system_database):
    """Appに認証ユーザーの削除を許可しない。"""
    async with system_database.connect("vector_app") as connection:
        with pytest.raises(asyncpg.InsufficientPrivilegeError) as failure:
            await connection.execute('DELETE FROM auth."user" WHERE false')
        assert failure.value.sqlstate == "42501"


async def test_app_cannot_delete_auth_rate_limit(system_database):
    """Appに認証rateLimitの削除を許可しない。"""
    async with system_database.connect("vector_app") as connection:
        with pytest.raises(asyncpg.InsufficientPrivilegeError) as failure:
            await connection.execute('DELETE FROM auth."rateLimit" WHERE false')
        assert failure.value.sqlstate == "42501"
