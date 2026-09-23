"""記事分析ロールの権限migrationの往復と、既存の権限・データの維持を確認する。"""

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

REVISION = "z25_grant_article_analysis"
PREDECESSOR = "z24_auth_cleanup_grants"
ROLE = "vector_article_analysis"
NO_GRANTS = (set(), {}, set(), [])
ROUND_TRIP = (
    (command.upgrade, REVISION),
    (command.downgrade, PREDECESSOR),
    (command.upgrade, REVISION),
)


@pytest.fixture
async def predecessor_database(system_database):
    await migrate(system_database, command.downgrade, PREDECESSOR)
    return system_database


async def _analysis_grants(database):
    async with database.connect(ROLE) as connection:
        schema_grants = await connection.fetch(
            "SELECT acl.privilege_type, acl.is_grantable "
            "FROM pg_namespace n CROSS JOIN LATERAL aclexplode(n.nspacl) acl "
            "WHERE n.nspname='public' AND acl.grantee=current_user::regrole"
        )
        return (
            await read_table_permissions(connection),
            await read_column_permissions(connection),
            await read_sequence_permissions(connection),
            [tuple(row) for row in schema_grants],
        )


async def test_analysis_grants_round_trip(predecessor_database):
    """付与した権限だけがdowngradeで消え、再upgradeで同じ権限へ戻る。"""
    database = predecessor_database
    assert await _analysis_grants(database) == NO_GRANTS

    await migrate(database, command.upgrade, REVISION)
    granted = await _analysis_grants(database)
    tables, columns, sequences, schema_grants = granted
    assert ("public", "analyzed_articles", "INSERT") in tables
    assert columns[("public", "analyzed_articles", "UPDATE")] == {"embedding"}
    assert sequences
    assert schema_grants == [("USAGE", False)]

    await migrate(database, command.downgrade, PREDECESSOR)
    assert await _analysis_grants(database) == NO_GRANTS

    await migrate(database, command.upgrade, REVISION)
    assert await _analysis_grants(database) == granted


async def _other_role_permissions(database):
    result = {}
    for role in (
        "vector_auth",
        "vector_app",
        "vector_collect",
        "vector_outbox_relay",
        "vector_auth_rate_limit_cleanup",
    ):
        async with database.connect(role) as connection:
            result[role] = (
                await read_table_permissions(connection),
                await read_column_permissions(connection),
                await read_sequence_permissions(connection),
            )
    return result


async def test_analysis_migration_preserves_other_role_permissions(
    predecessor_database,
):
    """vector_appを含む他のロールの実効権限を往復で変えない。"""
    database = predecessor_database
    before = await _other_role_permissions(database)
    for operation, revision in ROUND_TRIP:
        await migrate(database, operation, revision)
        assert await _other_role_permissions(database) == before


async def _analysis_rows(connection, article_id):
    return await connection.fetch(
        "SELECT a.*, c.id AS curation_id, c.translated_title AS curation_title, "
        "r.id AS analyzed_id, r.summary AS analyzed_summary "
        "FROM analyzable_articles a "
        "JOIN article_curations c ON c.analyzable_article_id=a.id "
        "JOIN analyzed_articles r ON r.curation_id=c.id "
        "WHERE a.id=$1",
        article_id,
    )


async def test_analysis_migration_preserves_existing_rows(predecessor_database):
    """権限を往復しても既存の記事・curation・分析結果を変更しない。"""
    database = predecessor_database
    async with database.connect("vector") as connection:
        article_id = await connection.fetchval(
            "INSERT INTO analyzable_articles "
            "(source_id, source_url, original_title, original_content, published_at) "
            "SELECT id, 'https://example.com/existing', 'title', 'content', now() "
            "FROM news_sources ORDER BY id LIMIT 1 RETURNING id"
        )
        curation_id = await connection.fetchval(
            "INSERT INTO article_curations "
            "(analyzable_article_id, translated_title, summary) "
            "VALUES ($1, '既存タイトル', '既存要約') RETURNING id",
            article_id,
        )
        await connection.execute(
            "INSERT INTO analyzed_articles "
            "(curation_id, translated_title, summary, investor_take, category_id) "
            "SELECT $1, '既存タイトル', '既存要約', '既存の判断', id "
            "FROM categories ORDER BY id LIMIT 1",
            curation_id,
        )
        before = await _analysis_rows(connection, article_id)
    assert len(before) == 1

    for operation, revision in ROUND_TRIP:
        await migrate(database, operation, revision)
        async with database.connect("vector") as connection:
            assert await _analysis_rows(connection, article_id) == before


async def test_missing_role_fails_before_granting(predecessor_database, monkeypatch):
    """クラスタ共通ロールを変更せず、存在しないロールで事前条件を検証する。"""
    path = ROOT / "backend/alembic/versions/z25_grant_article_analysis.py"
    spec = importlib.util.spec_from_file_location("article_analysis_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "ROLE_NAME", "vector_article_analysis_missing")
    engine = create_async_engine(predecessor_database.url("vector", sqlalchemy=True))

    def run(connection):
        with Operations.context(MigrationContext.configure(connection)):
            module.upgrade()

    try:
        with pytest.raises(RuntimeError, match="Create vector_article_analysis"):
            async with engine.begin() as connection:
                await connection.run_sync(run)
    finally:
        await engine.dispose()
    assert await _analysis_grants(predecessor_database) == NO_GRANTS
