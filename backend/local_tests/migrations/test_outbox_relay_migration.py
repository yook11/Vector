"""実際の直前revisionから配信権限migrationの互換性を確認する。"""

import pytest
from alembic.config import Config
from sqlalchemy.ext.asyncio import create_async_engine

from alembic import command
from local_tests.database import ROOT
from local_tests.permissions.support import read_column_permissions

REVISION = "z23_grant_outbox_relay"
PREDECESSOR = "z22_grant_collect_outbox"


async def _migrate(database, operation, revision):
    engine = create_async_engine(database.url("vector", sqlalchemy=True))

    def run(connection):
        config = Config(str(ROOT / "backend/alembic.ini"))
        config.set_main_option("script_location", str(ROOT / "backend/alembic"))
        config.attributes["connection"] = connection
        operation(config, revision)

    try:
        async with engine.connect() as connection:
            await connection.run_sync(run)
    finally:
        await engine.dispose()


@pytest.fixture
async def predecessor_database(system_database):
    await _migrate(system_database, command.downgrade, PREDECESSOR)
    async with system_database.connect("vector") as connection:
        assert await connection.fetchval("SELECT version_num FROM alembic_version") == (
            PREDECESSOR
        )
    return system_database


async def _relay_direct_schema_grants(database):
    async with database.connect("vector_outbox_relay") as connection:
        return await connection.fetch(
            "SELECT acl.privilege_type, acl.is_grantable "
            "FROM pg_namespace n CROSS JOIN LATERAL aclexplode(n.nspacl) acl "
            "WHERE n.nspname='public' AND acl.grantee=current_user::regrole "
            "ORDER BY acl.privilege_type"
        )


async def test_relay_grants_can_be_reversed_and_reapplied(predecessor_database):
    """配信用GRANTはdowngradeで消え再upgradeで同じ権限へ戻る。"""
    database = predecessor_database
    expected = {
        ("public", "outbox_events", "SELECT"): {
            "event_id",
            "event_type",
            "schema_version",
            "payload",
            "occurred_at",
            "published_at",
            "next_attempt_at",
            "attempt_count",
            "lease_token",
            "leased_until",
            "delivery_stopped_at",
        },
        ("public", "outbox_events", "UPDATE"): {
            "lease_token",
            "leased_until",
            "attempt_count",
            "published_at",
            "next_attempt_at",
            "delivery_stopped_at",
            "delivery_stop_reason",
        },
    }

    async with database.connect("vector_outbox_relay") as connection:
        assert await read_column_permissions(connection) == {}
    assert await _relay_direct_schema_grants(database) == []

    await _migrate(database, command.upgrade, REVISION)
    async with database.connect("vector_outbox_relay") as connection:
        assert await read_column_permissions(connection) == expected
    schema_grants = await _relay_direct_schema_grants(database)
    assert [tuple(row) for row in schema_grants] == [("USAGE", False)]

    await _migrate(database, command.downgrade, PREDECESSOR)
    async with database.connect("vector_outbox_relay") as connection:
        assert await read_column_permissions(connection) == {}
    assert await _relay_direct_schema_grants(database) == []

    await _migrate(database, command.upgrade, REVISION)
    async with database.connect("vector_outbox_relay") as connection:
        assert await read_column_permissions(connection) == expected
    assert await _relay_direct_schema_grants(database) == schema_grants


async def test_relay_migration_preserves_existing_events(predecessor_database):
    """権限migrationの往復で既存イベントの本文と配信状態を変えない。"""
    database = predecessor_database
    async with database.connect("vector") as connection:
        event = await connection.fetchrow(
            "INSERT INTO public.outbox_events "
            "(event_type,payload,attempt_count,lease_token,leased_until) VALUES "
            "('article.assessed_in_scope','{\"article_id\":42}',2,"
            "gen_random_uuid(),now()+interval '5 minutes') RETURNING *"
        )

    for operation, revision in (
        (command.upgrade, REVISION),
        (command.downgrade, PREDECESSOR),
        (command.upgrade, REVISION),
    ):
        await _migrate(database, operation, revision)
        async with database.connect("vector") as connection:
            assert (
                await connection.fetchrow(
                    "SELECT * FROM public.outbox_events WHERE event_id=$1",
                    event["event_id"],
                )
                == event
            )


async def _existing_acls(database):
    async with database.connect("vector") as connection:
        return await connection.fetch(
            "SELECT 'table' AS kind, c.oid AS object_id, 0 AS column_id, "
            "c.relacl::text AS acl FROM pg_class c JOIN pg_namespace n "
            "ON n.oid=c.relnamespace WHERE n.nspname IN ('public','auth') "
            "UNION ALL SELECT 'column', a.attrelid, a.attnum, "
            "ARRAY(SELECT entry FROM unnest(a.attacl) AS entry WHERE "
            "entry::text NOT LIKE 'vector_outbox_relay=%')::text "
            "FROM pg_attribute a JOIN pg_class c ON c.oid=a.attrelid "
            "JOIN pg_namespace n ON n.oid=c.relnamespace "
            "WHERE n.nspname IN ('public','auth') AND a.attnum>0 "
            "AND NOT a.attisdropped "
            "UNION ALL SELECT 'schema', n.oid, 0, "
            "ARRAY(SELECT entry FROM unnest(n.nspacl) AS entry WHERE "
            "entry::text NOT LIKE 'vector_outbox_relay=%')::text "
            "FROM pg_namespace n WHERE n.nspname IN ('public','auth') "
            "UNION ALL SELECT 'default', oid, 0, defaclacl::text "
            "FROM pg_default_acl "
            "ORDER BY kind, object_id, column_id"
        )


async def test_relay_migration_preserves_existing_role_grants(predecessor_database):
    """権限migrationの往復で既存ACLとdefault privilegesを変えない。"""
    database = predecessor_database
    before = await _existing_acls(database)

    for operation, revision in (
        (command.upgrade, REVISION),
        (command.downgrade, PREDECESSOR),
        (command.upgrade, REVISION),
    ):
        await _migrate(database, operation, revision)
        assert await _existing_acls(database) == before
