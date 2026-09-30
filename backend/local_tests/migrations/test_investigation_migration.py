"""調査ロールの権限migrationの往復と、既存の権限・データの維持を確認する。"""

import importlib.util

import asyncpg
import pytest
from alembic.operations import Operations
from alembic.runtime.migration import MigrationContext
from sqlalchemy.ext.asyncio import create_async_engine

from alembic import command
from local_tests.database import ROOT
from local_tests.migrations.support import migrate
from local_tests.permissions.support import (
    read_column_permissions,
    read_sequence_permissions,
    read_table_permissions,
)

REVISION = "z31_grant_investigation"
PREDECESSOR = "z30_grant_insights_publication"
ROLE = "vector_investigation"
NO_GRANTS = (set(), {}, set(), [], False, [])
ROUND_TRIP = (
    (command.upgrade, REVISION),
    (command.downgrade, PREDECESSOR),
    (command.upgrade, REVISION),
)


@pytest.fixture
async def predecessor_database(system_database):
    await migrate(system_database, command.downgrade, PREDECESSOR)
    return system_database


async def _investigation_grants(database):
    async with database.connect(ROLE) as connection:
        schema_grants = await connection.fetch(
            "SELECT acl.privilege_type, acl.is_grantable "
            "FROM pg_namespace n CROSS JOIN LATERAL aclexplode(n.nspacl) acl "
            "WHERE n.nspname='public' AND acl.grantee=current_user::regrole"
        )
        connect_granted = await connection.fetchval(
            "SELECT EXISTS(SELECT FROM pg_database d, "
            "LATERAL aclexplode(d.datacl) a WHERE d.datname=current_database() "
            "AND a.grantee=current_user::regrole "
            "AND a.privilege_type='CONNECT')"
        )
        default_grants = await connection.fetch(
            "SELECT pg_get_userbyid(d.defaclrole), n.nspname, d.defaclobjtype::text, "
            "acl.privilege_type, acl.is_grantable "
            "FROM pg_default_acl d JOIN pg_namespace n ON n.oid=d.defaclnamespace "
            "CROSS JOIN LATERAL aclexplode(d.defaclacl) acl "
            "WHERE acl.grantee=current_user::regrole"
        )
        return (
            await read_table_permissions(connection),
            await read_column_permissions(connection),
            await read_sequence_permissions(connection),
            [tuple(row) for row in schema_grants],
            connect_granted,
            [tuple(row) for row in default_grants],
        )


async def test_investigation_grants_round_trip(predecessor_database):
    """付与した権限と自動付与だけがdowngradeで消え、再upgradeで同じ権限へ戻る。"""
    database = predecessor_database
    assert await _investigation_grants(database) == NO_GRANTS

    await migrate(database, command.upgrade, REVISION)
    granted = await _investigation_grants(database)
    tables, _, sequences, schema_grants, connect_granted, default_grants = granted
    assert ("public", "categories", "SELECT") in tables
    assert sequences == set()
    assert schema_grants == [("USAGE", False)]
    assert connect_granted
    assert default_grants == [("vector", "public", "r", "SELECT", False)]

    await migrate(database, command.downgrade, PREDECESSOR)
    assert await _investigation_grants(database) == NO_GRANTS

    await migrate(database, command.upgrade, REVISION)
    assert await _investigation_grants(database) == granted


async def test_tables_created_after_upgrade_are_readable(predecessor_database):
    """upgrade後にvectorが作った表は読めて、downgrade後に作った表は読めない。"""
    database = predecessor_database
    await migrate(database, command.upgrade, REVISION)
    async with database.connect("vector") as connection:
        await connection.execute("CREATE TABLE public.after_upgrade (id integer)")
    async with database.connect(ROLE) as connection:
        assert await connection.fetch("SELECT id FROM public.after_upgrade") == []

    await migrate(database, command.downgrade, PREDECESSOR)
    async with database.connect("vector") as connection:
        await connection.execute("CREATE TABLE public.after_downgrade (id integer)")
    async with database.connect(ROLE) as connection:
        with pytest.raises(asyncpg.InsufficientPrivilegeError) as failure:
            await connection.fetch("SELECT id FROM public.after_downgrade")
        assert failure.value.sqlstate == "42501"


async def _other_role_permissions(database):
    result = {}
    for role in (
        "vector_auth",
        "vector_app",
        "vector_collect",
        "vector_outbox_relay",
        "vector_auth_rate_limit_cleanup",
        "vector_article_analysis",
        "vector_backfill",
        "vector_api",
        "vector_insights",
        "vector_agent",
    ):
        async with database.connect(role) as connection:
            result[role] = (
                await read_table_permissions(connection),
                await read_column_permissions(connection),
                await read_sequence_permissions(connection),
            )
    async with database.connect("vector") as connection:
        result["default"] = await connection.fetch(
            "SELECT pg_get_userbyid(d.defaclrole), n.nspname, d.defaclobjtype::text, "
            "pg_get_userbyid(acl.grantee), acl.privilege_type, acl.is_grantable "
            "FROM pg_default_acl d JOIN pg_namespace n ON n.oid=d.defaclnamespace "
            "CROSS JOIN LATERAL aclexplode(d.defaclacl) acl "
            "WHERE pg_get_userbyid(acl.grantee) <> $1 ORDER BY 1, 2, 3, 4, 5",
            ROLE,
        )
    return result


async def test_investigation_migration_preserves_other_role_permissions(
    predecessor_database,
):
    """他のロールの実効権限と、vector_appなどへの自動付与を往復で変えない。"""
    database = predecessor_database
    before = await _other_role_permissions(database)
    for operation, revision in ROUND_TRIP:
        await migrate(database, operation, revision)
        assert await _other_role_permissions(database) == before


async def test_investigation_migration_preserves_existing_rows(predecessor_database):
    """権限を往復しても既存のカテゴリを変更しない。"""
    database = predecessor_database
    async with database.connect("vector") as connection:
        before = await connection.fetch("SELECT * FROM categories ORDER BY id")
    assert before

    for operation, revision in ROUND_TRIP:
        await migrate(database, operation, revision)
        async with database.connect("vector") as connection:
            assert (
                await connection.fetch("SELECT * FROM categories ORDER BY id") == before
            )


async def test_missing_role_fails_before_granting(predecessor_database, monkeypatch):
    """クラスタ共通ロールを変更せず、存在しないロールで事前条件を検証する。"""
    path = ROOT / "backend/alembic/versions/z31_grant_investigation.py"
    spec = importlib.util.spec_from_file_location("investigation_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "ROLE_NAME", "vector_investigation_missing")
    engine = create_async_engine(predecessor_database.url("vector", sqlalchemy=True))

    def run(connection):
        with Operations.context(MigrationContext.configure(connection)):
            module.upgrade()

    try:
        with pytest.raises(RuntimeError, match="Create vector_investigation"):
            async with engine.begin() as connection:
                await connection.run_sync(run)
    finally:
        await engine.dispose()
    assert await _investigation_grants(predecessor_database) == NO_GRANTS
