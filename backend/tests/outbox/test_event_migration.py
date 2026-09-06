from __future__ import annotations

import importlib.util
from pathlib import Path
from uuid import uuid4

import pytest
from alembic.operations import Operations
from alembic.runtime.migration import MigrationContext
from sqlalchemy import inspect, text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.schema import CreateSchema

pytestmark = pytest.mark.asyncio


def _table_contract(connection: Connection, schema: str) -> dict:
    inspector = inspect(connection)
    columns = inspector.get_columns("outbox_events", schema=schema)
    return {
        "columns": [
            (c["name"], str(c["type"]), c["nullable"], c["default"]) for c in columns
        ],
        "primary_key": inspector.get_pk_constraint("outbox_events", schema=schema),
        "checks": inspector.get_check_constraints("outbox_events", schema=schema),
        "indexes": inspector.get_indexes("outbox_events", schema=schema),
    }


async def test_outbox_migration_round_trip_preserves_existing_data(
    db_session: AsyncSession,
) -> None:
    connection = await db_session.connection()
    model_contract = await connection.run_sync(lambda c: _table_contract(c, "public"))
    schema = "outbox_migration_" + uuid4().hex
    await connection.execute(CreateSchema(schema))
    await connection.execute(
        text("SELECT set_config('search_path', :schema, true)"), {"schema": schema}
    )
    # 直前revisionの関連境界を隔離し、新テーブル不在と既存の権限方針を再現する。
    await connection.execute(
        text(
            "ALTER DEFAULT PRIVILEGES FOR ROLE vector "
            "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO vector_app"
        )
    )
    await connection.execute(
        text("CREATE TABLE existing_data (id INTEGER PRIMARY KEY, value TEXT NOT NULL)")
    )
    await connection.execute(text("INSERT INTO existing_data VALUES (1, 'preserve')"))
    assert await connection.scalar(text("SELECT to_regclass('outbox_events')")) is None

    path = Path(__file__).parents[2] / "alembic/versions/z21_outbox_events.py"
    spec = importlib.util.spec_from_file_location("outbox_migration", path)
    assert spec is not None and spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    assert migration.revision == "z21_outbox_events"
    assert migration.down_revision == "z20_agent_run_answer_started_at"
    assert migration.MIGRATION_KIND == "expand"

    def migrate(sync_connection: Connection, operation: str) -> None:
        with Operations.context(MigrationContext.configure(sync_connection)):
            getattr(migration, operation)()

    for operation in ("upgrade", "downgrade", "upgrade"):
        await connection.run_sync(migrate, operation)
        assert (
            await connection.execute(text("SELECT id, value FROM existing_data"))
        ).all() == [(1, "preserve")]
        assert await connection.scalar(text("SHOW lock_timeout")) == "5s"
        assert await connection.scalar(text("SHOW statement_timeout")) == "5s"
        if operation == "downgrade":
            assert (
                await connection.scalar(text("SELECT to_regclass('outbox_events')"))
                is None
            )
            continue
        assert (
            await connection.run_sync(lambda c: _table_contract(c, schema))
            == model_contract
        )
        for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE"):
            assert (
                await connection.scalar(
                    text(
                        "SELECT has_table_privilege("
                        "'vector_app', 'outbox_events', :privilege)"
                    ),
                    {"privilege": privilege},
                )
                is True
            )
        await connection.execute(
            text(
                "INSERT INTO outbox_events (event_type, payload) "
                "VALUES ('article.curated', '{}')"
            )
        )
        assert await connection.scalar(text("SELECT count(*) FROM outbox_events")) == 1
