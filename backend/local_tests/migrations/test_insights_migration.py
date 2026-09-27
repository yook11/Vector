"""Insightsロールの権限migrationの往復と、既存の権限・データの維持を確認する。"""

import importlib.util

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

REVISION = "z28_grant_insights"
PREDECESSOR = "z27_grant_api"
ROLE = "vector_insights"
NO_GRANTS = (set(), {}, set(), [], False)
ROUND_TRIP = (
    (command.upgrade, REVISION),
    (command.downgrade, PREDECESSOR),
    (command.upgrade, REVISION),
)


@pytest.fixture
async def predecessor_database(system_database):
    await migrate(system_database, command.downgrade, PREDECESSOR)
    return system_database


async def _insights_grants(database):
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
        return (
            await read_table_permissions(connection),
            await read_column_permissions(connection),
            await read_sequence_permissions(connection),
            [tuple(row) for row in schema_grants],
            connect_granted,
        )


async def test_insights_grants_round_trip(predecessor_database):
    """付与した権限だけがdowngradeで消え、再upgradeで同じ権限へ戻る。"""
    database = predecessor_database
    assert await _insights_grants(database) == NO_GRANTS

    await migrate(database, command.upgrade, REVISION)
    granted = await _insights_grants(database)
    tables, columns, sequences, schema_grants, connect_granted = granted
    assert ("public", "weekly_briefings", "INSERT") in tables
    assert columns[("public", "pipeline_events", "SELECT")] == {"id", "occurred_at"}
    assert len(sequences) == 2
    assert schema_grants == [("USAGE", False)]
    assert connect_granted

    await migrate(database, command.downgrade, PREDECESSOR)
    assert await _insights_grants(database) == NO_GRANTS

    await migrate(database, command.upgrade, REVISION)
    assert await _insights_grants(database) == granted


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
    ):
        async with database.connect(role) as connection:
            result[role] = (
                await read_table_permissions(connection),
                await read_column_permissions(connection),
                await read_sequence_permissions(connection),
            )
    return result


async def test_insights_migration_preserves_other_role_permissions(
    predecessor_database,
):
    """vector_appを含む他のロールの実効権限を往復で変えない。"""
    database = predecessor_database
    before = await _other_role_permissions(database)
    for operation, revision in ROUND_TRIP:
        await migrate(database, operation, revision)
        assert await _other_role_permissions(database) == before


async def _insights_rows(connection):
    return (
        await connection.fetch("SELECT * FROM trends_snapshots ORDER BY window_end"),
        await connection.fetch("SELECT * FROM weekly_briefings ORDER BY id"),
    )


async def test_insights_migration_preserves_existing_rows(predecessor_database):
    """権限を往復しても既存のトレンドとブリーフィングを変更しない。"""
    database = predecessor_database
    async with database.connect("vector") as connection:
        await connection.execute(
            "INSERT INTO trends_snapshots "
            "(window_end, bundle, source_analysis_count, generated_at) "
            "VALUES ('2026-09-27', '{\"windowEnd\": \"2026-09-27\"}'::jsonb, 5, "
            "'2026-09-27T00:05:00+09:00')"
        )
        await connection.execute(
            "INSERT INTO weekly_briefings "
            "(week_start_date, category_id, headline, summary, chapters, "
            "key_articles, watch_points, model_name, input_article_count) "
            "SELECT '2026-09-21', id, '既存の見出し', '既存の要約', '[]'::jsonb, "
            "'[]'::jsonb, '[]'::jsonb, 'existing-model', 3 "
            "FROM categories ORDER BY id LIMIT 1"
        )
        before = await _insights_rows(connection)
    assert [len(rows) for rows in before] == [1, 1]

    for operation, revision in ROUND_TRIP:
        await migrate(database, operation, revision)
        async with database.connect("vector") as connection:
            assert await _insights_rows(connection) == before


async def test_missing_role_fails_before_granting(predecessor_database, monkeypatch):
    """クラスタ共通ロールを変更せず、存在しないロールで事前条件を検証する。"""
    path = ROOT / "backend/alembic/versions/z28_grant_insights.py"
    spec = importlib.util.spec_from_file_location("insights_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "ROLE_NAME", "vector_insights_missing")
    engine = create_async_engine(predecessor_database.url("vector", sqlalchemy=True))

    def run(connection):
        with Operations.context(MigrationContext.configure(connection)):
            module.upgrade()

    try:
        with pytest.raises(RuntimeError, match="Create vector_insights"):
            async with engine.begin() as connection:
                await connection.run_sync(run)
    finally:
        await engine.dispose()
    assert await _insights_grants(predecessor_database) == NO_GRANTS
