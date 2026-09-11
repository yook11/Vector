"""ユーザー作成の実DB動作テストへ置き換えるまで既存の契約を保持する。"""

import pytest

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def auth_conn(system_database):
    async with system_database.connect("vector_auth") as connection:
        yield connection


async def test_auth_tables_match_better_auth_provisioning_schema(auth_conn) -> None:
    """provisioning が書く Better Auth の必須列・型・nullability を固定する。"""
    rows = await auth_conn.fetch(
        "SELECT cls.relname AS table_name, attr.attname AS column_name, "
        "format_type(attr.atttypid, attr.atttypmod) AS column_type, "
        "attr.attnotnull AS not_null "
        "FROM pg_catalog.pg_attribute AS attr "
        "JOIN pg_catalog.pg_class AS cls ON cls.oid = attr.attrelid "
        "JOIN pg_catalog.pg_namespace AS ns ON ns.oid = cls.relnamespace "
        "WHERE ns.nspname = 'auth' "
        "AND cls.relname IN ('user', 'account') "
        "AND attr.attnum > 0 "
        "AND NOT attr.attisdropped"
    )
    actual = {
        (row["table_name"], row["column_name"]): (
            row["column_type"],
            row["not_null"],
        )
        for row in rows
    }
    expected = {
        ("user", "id"): ("uuid", True),
        ("user", "name"): ("text", True),
        ("user", "email"): ("text", True),
        ("user", "emailVerified"): ("boolean", True),
        ("user", "createdAt"): ("timestamp with time zone", True),
        ("user", "updatedAt"): ("timestamp with time zone", True),
        ("user", "role"): ("text", True),
        ("account", "id"): ("uuid", True),
        ("account", "accountId"): ("text", True),
        ("account", "providerId"): ("text", True),
        ("account", "userId"): ("uuid", True),
        ("account", "password"): ("text", False),
        ("account", "createdAt"): ("timestamp with time zone", True),
        ("account", "updatedAt"): ("timestamp with time zone", True),
    }

    assert {key: actual.get(key) for key in expected} == expected


async def test_auth_user_email_unique_constraint_matches_provisioning_contract(
    auth_conn,
) -> None:
    """duplicate-email 分類の根拠の user_email_key を pg_constraint で固定する。"""
    row = await auth_conn.fetchrow(
        "SELECT constraint_def.contype::text AS contype, "
        "array_agg(attr.attname ORDER BY key_position.ordinality) AS columns "
        "FROM pg_catalog.pg_constraint AS constraint_def "
        "JOIN pg_catalog.pg_class AS cls ON cls.oid = constraint_def.conrelid "
        "JOIN pg_catalog.pg_namespace AS ns ON ns.oid = cls.relnamespace "
        "JOIN unnest(constraint_def.conkey) WITH ORDINALITY "
        "AS key_position(attnum, ordinality) ON true "
        "JOIN pg_catalog.pg_attribute AS attr "
        "ON attr.attrelid = cls.oid AND attr.attnum = key_position.attnum "
        "WHERE ns.nspname = 'auth' "
        "AND cls.relname = 'user' "
        "AND constraint_def.conname = 'user_email_key' "
        "GROUP BY constraint_def.contype"
    )

    assert row is not None
    assert row["contype"] == "u"
    assert row["columns"] == ["email"]
