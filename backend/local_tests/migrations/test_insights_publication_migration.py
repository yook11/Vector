"""記事2表のSELECTだけを追加・撤去できることを実DBで確認する。"""

import importlib.util

import pytest
from alembic.operations import Operations
from alembic.runtime.migration import MigrationContext
from sqlalchemy.ext.asyncio import create_async_engine

from alembic import command
from local_tests.database import ROOT
from local_tests.insights.support import seed_analyzed_article, seeded_categories
from local_tests.migrations.support import migrate
from local_tests.migrations.test_insights_migration import _insights_grants
from local_tests.permissions.support import (
    read_column_permissions,
    read_sequence_permissions,
    read_table_columns,
    read_table_permissions,
)
from local_tests.permissions.test_role_boundaries import ROLES

REVISION = "z30_grant_insights_publication"
PREDECESSOR = "z29_grant_agent"
ROUND_TRIP = (
    (command.upgrade, REVISION),
    (command.downgrade, PREDECESSOR),
    (command.upgrade, REVISION),
)


@pytest.fixture
async def predecessor_database(system_database):
    await migrate(system_database, command.downgrade, PREDECESSOR)
    return system_database


async def test_publication_grants_round_trip_preserves_prior_permissions(
    predecessor_database,
):
    """記事2表のSELECTだけが増減し、既存のInsights権限は維持される。"""
    database = predecessor_database
    before = await _insights_grants(database)
    async with database.connect("vector_insights") as connection:
        columns = await read_table_columns(connection)
    expected_columns = {
        **before[1],
        ("public", "article_curations", "SELECT"): columns[
            ("public", "article_curations")
        ],
        ("public", "analyzable_articles", "SELECT"): columns[
            ("public", "analyzable_articles")
        ],
    }
    expected_tables = before[0] | {
        ("public", "article_curations", "SELECT"),
        ("public", "analyzable_articles", "SELECT"),
    }
    expected = (expected_tables, expected_columns, *before[2:])
    for operation, revision in ROUND_TRIP:
        await migrate(database, operation, revision)
        actual = await _insights_grants(database)
        assert actual == (before if revision == PREDECESSOR else expected)


async def test_publication_migration_preserves_other_runtime_permissions(
    predecessor_database,
):
    """agentを含む他の実行ロールの権限は往復で変わらない。"""
    database = predecessor_database

    async def permissions():
        values = {}
        for role in ROLES:
            if role == "vector_insights":
                continue
            async with database.connect(role) as connection:
                values[role] = (
                    await read_table_permissions(connection),
                    await read_column_permissions(connection),
                    await read_sequence_permissions(connection),
                )
        return values

    before = await permissions()
    for operation, revision in ROUND_TRIP:
        await migrate(database, operation, revision)
        assert await permissions() == before


async def test_publication_migration_preserves_article_rows(predecessor_database):
    """権限migrationの往復で関連する3表の記事データを変更しない。"""
    from datetime import UTC, datetime

    database = predecessor_database
    category = (await seeded_categories(database))[0]
    await seed_analyzed_article(
        database,
        category_id=category.id,
        title="publication-migration",
        analyzed_at=datetime(2026, 9, 27, tzinfo=UTC),
    )

    async def rows():
        async with database.connect("vector") as connection:
            return (
                await connection.fetch("SELECT * FROM analyzable_articles ORDER BY id"),
                await connection.fetch("SELECT * FROM article_curations ORDER BY id"),
                await connection.fetch("SELECT * FROM analyzed_articles ORDER BY id"),
            )

    before = await rows()
    for operation, revision in ROUND_TRIP:
        await migrate(database, operation, revision)
        assert await rows() == before


async def test_missing_role_stops_before_publication_grants(
    predecessor_database, monkeypatch
):
    """ロールがない場合は既存の権限を変えず停止する。"""
    database = predecessor_database
    before = await _insights_grants(database)
    path = ROOT / "backend/alembic/versions/z30_grant_insights_publication.py"
    spec = importlib.util.spec_from_file_location("publication_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "ROLE_NAME", "vector_insights_missing")
    engine = create_async_engine(database.url("vector", sqlalchemy=True))

    def run(connection):
        with Operations.context(MigrationContext.configure(connection)):
            module.upgrade()

    try:
        with pytest.raises(RuntimeError, match="Create vector_insights"):
            async with engine.begin() as connection:
                await connection.run_sync(run)
    finally:
        await engine.dispose()
    assert await _insights_grants(database) == before
