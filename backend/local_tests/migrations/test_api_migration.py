"""APIロールの権限migrationの往復と、既存の権限・データの維持を確認する。"""

import importlib.util
from datetime import UTC, date, datetime
from uuid import uuid4

import pytest
from alembic.operations import Operations
from alembic.runtime.migration import MigrationContext
from sqlalchemy.ext.asyncio import create_async_engine

from alembic import command
from local_tests.api.support import (
    seed_article,
    seed_daily_quota,
    seed_news_source,
    seed_question,
    seed_thread,
    seeded_categories,
    watch,
)
from local_tests.database import ROOT
from local_tests.migrations.support import migrate
from local_tests.permissions.support import (
    read_column_permissions,
    read_sequence_permissions,
    read_table_permissions,
)

REVISION = "z27_grant_api"
PREDECESSOR = "z26_grant_backfill"
ROLE = "vector_api"
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


async def _api_grants(database):
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


async def test_api_grants_round_trip(predecessor_database):
    """付与した権限だけがdowngradeで消え、再upgradeで同じ権限へ戻る。"""
    database = predecessor_database
    assert await _api_grants(database) == NO_GRANTS

    await migrate(database, command.upgrade, REVISION)
    granted = await _api_grants(database)
    tables, columns, sequences, schema_grants, connect_granted = granted
    assert ("public", "watchlist_entries", "DELETE") in tables
    assert columns[("public", "agent_runs", "UPDATE")] == {"status", "error_code"}
    assert sequences
    assert schema_grants == [("USAGE", False)]
    assert connect_granted

    await migrate(database, command.downgrade, PREDECESSOR)
    assert await _api_grants(database) == NO_GRANTS

    await migrate(database, command.upgrade, REVISION)
    assert await _api_grants(database) == granted


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
    ):
        async with database.connect(role) as connection:
            result[role] = (
                await read_table_permissions(connection),
                await read_column_permissions(connection),
                await read_sequence_permissions(connection),
            )
    return result


async def test_api_migration_preserves_other_role_permissions(predecessor_database):
    """vector_appを含む他のロールの実効権限を往復で変えない。"""
    database = predecessor_database
    before = await _other_role_permissions(database)
    for operation, revision in ROUND_TRIP:
        await migrate(database, operation, revision)
        assert await _other_role_permissions(database) == before


async def _api_rows(connection, user_id, source_id):
    return (
        await connection.fetch("SELECT * FROM news_sources WHERE id=$1", source_id),
        await connection.fetch(
            "SELECT * FROM watchlist_entries WHERE user_id=$1", user_id
        ),
        await connection.fetch("SELECT * FROM agent_threads WHERE user_id=$1", user_id),
        await connection.fetch(
            "SELECT m.* FROM agent_messages m "
            "JOIN agent_threads t ON t.id=m.thread_id WHERE t.user_id=$1",
            user_id,
        ),
        await connection.fetch(
            "SELECT r.* FROM agent_runs r "
            "JOIN agent_threads t ON t.id=r.thread_id WHERE t.user_id=$1",
            user_id,
        ),
        await connection.fetch(
            "SELECT * FROM agent_user_daily_quotas WHERE user_id=$1", user_id
        ),
    )


async def test_api_migration_preserves_existing_rows(predecessor_database):
    """権限を往復しても既存のソース・ウォッチ・スレッド・run・利用枠を変更しない。"""
    database = predecessor_database
    user_id = uuid4()
    async with database.connect("vector") as connection:
        await connection.execute(
            'INSERT INTO auth."user" '
            '(id, name, email, "emailVerified", "createdAt", "updatedAt", role) '
            "VALUES ($1, 'API test', 'api@example.test', false, now(), now(), 'user')",
            user_id,
        )
    source = await seed_news_source(
        database,
        name="Existing Source",
        endpoint_url="https://existing.example.com/feed.xml",
    )
    [category, *_] = await seeded_categories(database)
    article_id = await seed_article(
        database,
        source=source,
        category=category,
        source_url="https://existing.example.com/article",
        title="既存の記事",
    )
    await watch(
        database,
        user_id=user_id,
        article_id=article_id,
        watched_at=datetime(2026, 9, 20, tzinfo=UTC),
    )
    thread_id = await seed_thread(database, user_id=user_id, title="既存のスレッド")
    await seed_question(
        database,
        thread_id=thread_id,
        seq=1,
        content="既存の質問",
        status="queued",
        quota_usage_date=date(2026, 9, 20),
    )
    await seed_daily_quota(
        database, user_id=user_id, usage_date=date(2026, 9, 20), used_count=1
    )
    async with database.connect("vector") as connection:
        before = await _api_rows(connection, user_id, source.id)
    assert [len(rows) for rows in before] == [1, 1, 1, 1, 1, 1]

    for operation, revision in ROUND_TRIP:
        await migrate(database, operation, revision)
        async with database.connect("vector") as connection:
            assert await _api_rows(connection, user_id, source.id) == before


async def test_missing_role_fails_before_granting(predecessor_database, monkeypatch):
    """クラスタ共通ロールを変更せず、存在しないロールで事前条件を検証する。"""
    path = ROOT / "backend/alembic/versions/z27_grant_api.py"
    spec = importlib.util.spec_from_file_location("api_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "ROLE_NAME", "vector_api_missing")
    engine = create_async_engine(predecessor_database.url("vector", sqlalchemy=True))

    def run(connection):
        with Operations.context(MigrationContext.configure(connection)):
            module.upgrade()

    try:
        with pytest.raises(RuntimeError, match="Create vector_api"):
            async with engine.begin() as connection:
                await connection.run_sync(run)
    finally:
        await engine.dispose()
    assert await _api_grants(predecessor_database) == NO_GRANTS
