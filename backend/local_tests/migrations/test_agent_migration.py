"""agentロールの権限migrationの往復と、既存の権限・データの維持を確認する。"""

import importlib.util
from datetime import UTC, date, datetime
from uuid import uuid4

import pytest
from alembic.operations import Operations
from alembic.runtime.migration import MigrationContext
from sqlalchemy.ext.asyncio import create_async_engine

from alembic import command
from local_tests.api.support import (
    embedding,
    seed_answer,
    seed_daily_quota,
    seed_external_source,
    seed_question,
    seed_thread,
)
from local_tests.database import ROOT
from local_tests.migrations.support import migrate
from local_tests.permissions.support import (
    read_column_permissions,
    read_sequence_permissions,
    read_table_permissions,
)

REVISION = "z29_grant_agent"
PREDECESSOR = "z28_grant_insights"
ROLE = "vector_agent"
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


async def _agent_grants(database):
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


async def test_agent_grants_round_trip(predecessor_database):
    """付与した権限だけがdowngradeで消え、再upgradeで同じ権限へ戻る。"""
    database = predecessor_database
    assert await _agent_grants(database) == NO_GRANTS

    await migrate(database, command.upgrade, REVISION)
    granted = await _agent_grants(database)
    tables, columns, sequences, schema_grants, connect_granted = granted
    assert ("public", "agent_runs", "UPDATE") in tables
    assert columns[("public", "agent_threads", "UPDATE")] == {
        "updated_at",
        "research_handoff",
    }
    assert len(sequences) == 2
    assert schema_grants == [("USAGE", False)]
    assert connect_granted

    await migrate(database, command.downgrade, PREDECESSOR)
    assert await _agent_grants(database) == NO_GRANTS

    await migrate(database, command.upgrade, REVISION)
    assert await _agent_grants(database) == granted


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
    ):
        async with database.connect(role) as connection:
            result[role] = (
                await read_table_permissions(connection),
                await read_column_permissions(connection),
                await read_sequence_permissions(connection),
            )
    return result


async def test_agent_migration_preserves_other_role_permissions(
    predecessor_database,
):
    """vector_appを含む他のロールの実効権限を往復で変えない。"""
    database = predecessor_database
    before = await _other_role_permissions(database)
    for operation, revision in ROUND_TRIP:
        await migrate(database, operation, revision)
        assert await _other_role_permissions(database) == before


async def _agent_rows(connection, user_id):
    return (
        await connection.fetch("SELECT * FROM agent_threads WHERE user_id=$1", user_id),
        await connection.fetch(
            "SELECT m.* FROM agent_messages m "
            "JOIN agent_threads t ON t.id=m.thread_id WHERE t.user_id=$1 "
            "ORDER BY m.seq",
            user_id,
        ),
        await connection.fetch(
            "SELECT s.* FROM agent_message_sources s "
            "JOIN agent_messages m ON m.id=s.message_id "
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
        await connection.fetch("SELECT * FROM query_embedding_cache ORDER BY id"),
    )


async def test_agent_migration_preserves_existing_rows(predecessor_database):
    """権限を往復しても既存のスレッド・回答・出典・run・利用枠・埋め込みを変更しない。"""
    database = predecessor_database
    user_id = uuid4()
    async with database.connect("vector") as connection:
        await connection.execute(
            'INSERT INTO auth."user" '
            '(id, name, email, "emailVerified", "createdAt", "updatedAt", role) '
            "VALUES ($1, 'Agent test', 'agent@example.test', false, now(), now(), "
            "'user')",
            user_id,
        )
        await connection.execute(
            "INSERT INTO query_embedding_cache "
            "(query_hash, embedder_identity, query_vector) "
            "VALUES ($1, 'existing-embedder', $2::text::halfvec)",
            "0" * 64,
            embedding(1.0, 0.0),
        )
    thread_id = await seed_thread(database, user_id=user_id, title="既存のスレッド")
    question = await seed_question(
        database,
        thread_id=thread_id,
        seq=1,
        content="既存の質問",
        status="running",
        quota_usage_date=date(2026, 9, 20),
    )
    answer_id = await seed_answer(
        database,
        thread_id=thread_id,
        run_id=question.run_id,
        seq=2,
        content="既存の回答[[1]]",
    )
    await seed_external_source(
        database,
        message_id=answer_id,
        source_ref="1",
        url="https://existing.example.com/report",
        title="既存の出典",
        source_name="Existing",
        published_at=datetime(2026, 9, 19, tzinfo=UTC),
        evidence_claim="既存の根拠",
    )
    await seed_daily_quota(
        database, user_id=user_id, usage_date=date(2026, 9, 20), used_count=1
    )
    async with database.connect("vector") as connection:
        before = await _agent_rows(connection, user_id)
    assert [len(rows) for rows in before] == [1, 2, 1, 1, 1, 1]

    for operation, revision in ROUND_TRIP:
        await migrate(database, operation, revision)
        async with database.connect("vector") as connection:
            assert await _agent_rows(connection, user_id) == before


async def test_missing_role_fails_before_granting(predecessor_database, monkeypatch):
    """クラスタ共通ロールを変更せず、存在しないロールで事前条件を検証する。"""
    path = ROOT / "backend/alembic/versions/z29_grant_agent.py"
    spec = importlib.util.spec_from_file_location("agent_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "ROLE_NAME", "vector_agent_missing")
    engine = create_async_engine(predecessor_database.url("vector", sqlalchemy=True))

    def run(connection):
        with Operations.context(MigrationContext.configure(connection)):
            module.upgrade()

    try:
        with pytest.raises(RuntimeError, match="Create vector_agent"):
            async with engine.begin() as connection:
                await connection.run_sync(run)
    finally:
        await engine.dispose()
    assert await _agent_grants(predecessor_database) == NO_GRANTS
