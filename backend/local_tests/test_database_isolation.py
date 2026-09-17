"""共通DB基盤のケース分離と失敗時の回収を確認する。"""

from dataclasses import replace

import asyncpg
import pytest

from local_tests.database import isolated_database

pytestmark = pytest.mark.asyncio


async def test_clone_preserves_migrated_database_permissions(system_database_template):
    """DB単位の権限はmigration済みの複製元と完全に一致する。"""
    query = (
        "SELECT a.grantor, a.grantee, a.privilege_type, a.is_grantable "
        "FROM pg_database d CROSS JOIN LATERAL "
        "aclexplode(coalesce(d.datacl, acldefault('d',d.datdba))) a "
        "WHERE d.datname=$1 "
        "ORDER BY a.grantor, a.grantee, a.privilege_type"
    )
    admin = replace(system_database_template, name="postgres")
    async with admin.connect("vector") as connection:
        expected = await connection.fetch(query, "vector")
    async with isolated_database(system_database_template) as database:
        async with database.connect("vector") as connection:
            assert await connection.fetch(query, database.name) == expected


async def test_committed_data_and_sequence_do_not_leak_between_cases(
    system_database_template,
):
    """commit済みの変更を捨ててもmigration由来の初期データを保持する。"""
    async with isolated_database(system_database_template) as first:
        async with first.connect("vector_app") as connection:
            baseline = await connection.fetchval("SELECT count(*) FROM categories")
            first_id = await connection.fetchval(
                "INSERT INTO categories (slug, name) "
                "VALUES ('system_test', 'system test') RETURNING id"
            )
    async with isolated_database(system_database_template) as second:
        async with second.connect("vector_app") as connection:
            assert (
                await connection.fetchval("SELECT count(*) FROM categories") == baseline
            )
            second_id = await connection.fetchval(
                "INSERT INTO categories (slug, name) "
                "VALUES ('system_test', 'system test') RETURNING id"
            )
            assert second_id == first_id


async def test_failed_case_drops_its_database_and_allows_next_case(
    system_database_template,
):
    """失敗したケースの未解放接続を回収して、次のケースを初期状態で開始する。"""
    connection = None
    try:
        with pytest.raises(RuntimeError, match="case failed"):
            async with isolated_database(system_database_template) as database:
                connection = await asyncpg.connect(database.url("vector_app"))
                await connection.execute(
                    "INSERT INTO categories (slug, name) "
                    "VALUES ('failed_case', 'failed case')"
                )
                raise RuntimeError("case failed")
        async with isolated_database(system_database_template) as database:
            async with database.connect("vector_app") as next_connection:
                assert (
                    await next_connection.fetchval(
                        "SELECT count(*) FROM categories WHERE slug='failed_case'"
                    )
                    == 0
                )
    finally:
        if connection is not None:
            await connection.close()
