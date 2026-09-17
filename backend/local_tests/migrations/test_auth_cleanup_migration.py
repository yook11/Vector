"""掃除権限migrationの往復と失敗時の原子性を確認する。"""

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

REVISION = "z24_auth_cleanup_grants"
PREDECESSOR = "z23_grant_outbox_relay"
ROLE = "vector_auth_rate_limit_cleanup"


@pytest.fixture
async def predecessor_database(system_database):
    await migrate(system_database, command.downgrade, PREDECESSOR)
    return system_database


async def _assert_no_cleanup_grants(database):
    async with database.connect(ROLE) as connection:
        assert await read_table_permissions(connection) == set()
        assert await read_column_permissions(connection) == {}
        assert not await connection.fetchval(
            "SELECT has_schema_privilege('auth','USAGE')"
        )
        assert not await connection.fetchval(
            "SELECT EXISTS(SELECT FROM pg_database d, "
            "LATERAL aclexplode(d.datacl) a WHERE d.datname=current_database() "
            "AND a.grantee=current_user::regrole "
            "AND a.privilege_type='CONNECT')"
        )
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await connection.execute('DELETE FROM auth."rateLimit" WHERE false')


async def test_cleanup_grants_round_trip(predecessor_database):
    """直接付与した権限だけが往復に合わせて有効・無効になる。"""
    database = predecessor_database
    await _assert_no_cleanup_grants(database)
    for operation, revision in (
        (command.upgrade, REVISION),
        (command.downgrade, PREDECESSOR),
        (command.upgrade, REVISION),
    ):
        await migrate(database, operation, revision)
        if operation is command.downgrade:
            await _assert_no_cleanup_grants(database)
        else:
            async with database.connect(ROLE) as connection:
                assert await read_table_permissions(connection) == {
                    ("auth", "rateLimit", "DELETE")
                }
                assert await read_column_permissions(connection) == {
                    ("auth", "rateLimit", "SELECT"): {"lastRequest"}
                }
                assert await connection.fetchval(
                    "SELECT EXISTS(SELECT FROM pg_database d, "
                    "LATERAL aclexplode(d.datacl) a WHERE d.datname=current_database() "
                    "AND a.grantee=current_user::regrole "
                    "AND a.privilege_type='CONNECT')"
                )


async def _existing_permissions(database):
    result = {}
    for role in ("vector_auth", "vector_app", "vector_collect", "vector_outbox_relay"):
        async with database.connect(role) as connection:
            result[role] = (
                await read_table_permissions(connection),
                await read_column_permissions(connection),
                await read_sequence_permissions(connection),
            )
    return result


async def test_cleanup_migration_preserves_existing_permissions(predecessor_database):
    """既存アプリの実効権限を権限migrationで変えない。"""
    database = predecessor_database
    before = await _existing_permissions(database)
    for operation, revision in (
        (command.upgrade, REVISION),
        (command.downgrade, PREDECESSOR),
        (command.upgrade, REVISION),
    ):
        await migrate(database, operation, revision)
        assert await _existing_permissions(database) == before


async def test_cleanup_migration_preserves_counters(predecessor_database):
    """権限を往復しても既存のカウンターを削除・更新しない。"""
    database = predecessor_database
    async with database.connect("vector") as connection:
        before = await connection.fetchrow(
            'INSERT INTO auth."rateLimit" ("id","key","count","lastRequest") '
            "VALUES (gen_random_uuid(),'existing',3,0) RETURNING *"
        )
    for operation, revision in (
        (command.upgrade, REVISION),
        (command.downgrade, PREDECESSOR),
        (command.upgrade, REVISION),
    ):
        await migrate(database, operation, revision)
        async with database.connect("vector") as connection:
            assert await connection.fetchrow('SELECT * FROM auth."rateLimit"') == before


@pytest.mark.parametrize(
    ("statement", "message"),
    [
        ('ALTER TABLE auth."rateLimit" RENAME TO missing_rate_limit', "Create auth"),
        (
            'ALTER TABLE auth."rateLimit" RENAME COLUMN "lastRequest" TO missing',
            'requires "lastRequest"',
        ),
    ],
)
async def test_missing_auth_contract_leaves_no_grants(
    predecessor_database, statement, message
):
    """対象表・列がなければrevisionも権限も進めない。"""
    database = predecessor_database
    async with database.connect("vector") as connection:
        await connection.execute(statement)
    with pytest.raises(RuntimeError, match=message):
        await migrate(database, command.upgrade, REVISION)
    async with database.connect(ROLE) as connection:
        assert await read_table_permissions(connection) == set()
        assert await read_column_permissions(connection) == {}
        assert not await connection.fetchval(
            "SELECT has_schema_privilege('auth','USAGE')"
        )
    async with database.connect("vector") as connection:
        assert (
            await connection.fetchval("SELECT version_num FROM alembic_version")
            == PREDECESSOR
        )


async def test_missing_role_fails_before_granting(predecessor_database, monkeypatch):
    """クラスタ共通ロールを変更せず存在しないロールで事前条件を検証する。"""
    path = ROOT / "backend/alembic/versions/z24_auth_cleanup_grants.py"
    spec = importlib.util.spec_from_file_location("cleanup_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "ROLE_NAME", "vector_cleanup_role_missing")
    engine = create_async_engine(predecessor_database.url("vector", sqlalchemy=True))

    def run(connection):
        with Operations.context(MigrationContext.configure(connection)):
            module.upgrade()

    try:
        with pytest.raises(RuntimeError, match="Create vector_auth_rate_limit_cleanup"):
            async with engine.begin() as connection:
                await connection.run_sync(run)
    finally:
        await engine.dispose()
    await _assert_no_cleanup_grants(predecessor_database)
