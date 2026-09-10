from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType
from uuid import uuid4

import pytest
from alembic.operations import Operations
from alembic.runtime.migration import MigrationContext
from sqlalchemy import Connection, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.schema import CreateSchema

from app.models.outbox_event import OutboxEvent

pytestmark = pytest.mark.asyncio


def _load(name: str) -> ModuleType:
    path = Path(__file__).parents[2] / "alembic/versions" / (name + ".py")
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _migrate(connection: Connection, module: ModuleType, operation: str) -> None:
    with Operations.context(MigrationContext.configure(connection)):
        getattr(module, operation)()


async def test_collect_outbox_grant_round_trip_and_orm_contract(
    db_session: AsyncSession,
) -> None:
    connection = await db_session.connection()
    schema = "collect_outbox_" + uuid4().hex
    await connection.execute(CreateSchema(schema))
    await connection.execute(
        text("SELECT set_config('search_path', :schema, true)"), {"schema": schema}
    )
    # publicの現行モデルではなく、直前revisionの表とcollect権限なしを再現する。
    await connection.run_sync(_migrate, _load("z21_outbox_events"), "upgrade")
    await connection.execute(
        text(
            "DO $$ BEGIN EXECUTE format('GRANT USAGE ON SCHEMA %I TO "
            "vector_collect', current_schema()); END $$"
        )
    )
    migration = _load("z22_grant_collect_outbox")
    assert migration.down_revision == "z21_outbox_events"
    assert migration.MIGRATION_KIND == "contract"
    grant_columns = {
        "event_id",
        "schema_version",
        "occurred_at",
        "next_attempt_at",
        "attempt_count",
    }
    assert not await connection.scalar(
        text("SELECT has_table_privilege('vector_collect', 'outbox_events', 'INSERT')")
    )
    for operation in ("upgrade", "downgrade", "upgrade"):
        await connection.run_sync(_migrate, migration, operation)
        enabled = operation == "upgrade"
        assert (
            await connection.scalar(
                text(
                    "SELECT has_table_privilege('vector_collect', "
                    "'outbox_events', 'INSERT')"
                )
            )
            is enabled
        )
        for column in OutboxEvent.__table__.columns:
            assert await connection.scalar(
                text(
                    "SELECT has_column_privilege('vector_collect', "
                    "'outbox_events', :column, 'SELECT')"
                ),
                {"column": column.name},
            ) is (enabled and column.name in grant_columns)
        for privilege in ("SELECT", "UPDATE", "DELETE", "TRUNCATE"):
            assert not await connection.scalar(
                text(
                    "SELECT has_table_privilege('vector_collect', "
                    "'outbox_events', :privilege)"
                ),
                {"privilege": privilege},
            )
        assert await connection.scalar(text("SHOW lock_timeout")) == "5s"
        if not enabled:
            assert (
                await connection.scalar(text("SELECT count(*) FROM outbox_events")) == 2
            )
            continue
        if await connection.scalar(text("SELECT count(*) FROM outbox_events")):
            continue
        await connection.execute(text("SET LOCAL ROLE vector_collect"))
        async with AsyncSession(
            bind=connection, join_transaction_mode="create_savepoint"
        ) as session:
            events = [
                OutboxEvent(
                    event_type="article.acquired", schema_version=1, payload={}
                ),
                OutboxEvent(
                    event_type="article.incomplete_recorded",
                    schema_version=1,
                    payload={},
                ),
            ]
            session.add_all(events)
            await session.flush()
            assert all(
                e.event_id and e.occurred_at and e.next_attempt_at for e in events
            )
            assert all(e.attempt_count == 0 for e in events)
            await session.commit()
        for statement in (
            "SELECT payload FROM outbox_events",
            "SELECT published_at FROM outbox_events",
            "UPDATE outbox_events SET published_at = now()",
            "DELETE FROM outbox_events",
            "TRUNCATE outbox_events",
        ):
            with pytest.raises(DBAPIError) as failure:
                async with connection.begin_nested():
                    await connection.execute(text(statement))
            assert failure.value.orig.sqlstate == "42501"
        await connection.execute(text("RESET ROLE"))
    assert await connection.scalar(text("SELECT count(*) FROM outbox_events")) == 2
