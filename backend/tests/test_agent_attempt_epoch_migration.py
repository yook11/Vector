"""attempt_epoch migrationのbackfillと往復契約。"""

from __future__ import annotations

import importlib.util
import uuid
from pathlib import Path
from types import ModuleType

import pytest
from alembic.operations import Operations
from alembic.runtime.migration import MigrationContext
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

MIGRATION_PATH = (
    Path(__file__).parents[1]
    / "alembic"
    / "versions"
    / "y3_agent_runs_attempt_epoch.py"
)


def _load_migration() -> ModuleType:
    assert MIGRATION_PATH.exists(), f"missing migration: {MIGRATION_PATH}"
    spec = importlib.util.spec_from_file_location(
        "test_y3_agent_runs_attempt_epoch",
        MIGRATION_PATH,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def _invoke_migration(
    connection: AsyncConnection,
    migration: ModuleType,
    operation: str,
) -> None:
    def invoke(sync_connection: object) -> None:
        context = MigrationContext.configure(sync_connection)  # type: ignore[arg-type]
        migration.op = Operations(context)
        getattr(migration, operation)()

    await connection.run_sync(invoke)


async def _read_column_contract(connection: AsyncConnection) -> tuple[str, bool, str]:
    return (
        await connection.execute(
            text(
                """
                SELECT format_type(attribute.atttypid, attribute.atttypmod),
                       attribute.attnotnull,
                       pg_get_expr(default_value.adbin, default_value.adrelid)
                FROM pg_attribute AS attribute
                LEFT JOIN pg_attrdef AS default_value
                  ON default_value.adrelid = attribute.attrelid
                 AND default_value.adnum = attribute.attnum
                WHERE attribute.attrelid = 'agent_runs'::regclass
                  AND attribute.attname = 'attempt_epoch'
                  AND NOT attribute.attisdropped
                """
            )
        )
    ).one()


async def _read_check_definition(connection: AsyncConnection) -> str:
    return (
        await connection.execute(
            text(
                """
                SELECT pg_get_constraintdef(oid)
                FROM pg_constraint
                WHERE conrelid = 'agent_runs'::regclass
                  AND conname = 'ck_agent_runs_attempt_epoch_nonnegative'
                """
            )
        )
    ).scalar_one()


@pytest.mark.asyncio
async def test_attempt_epoch_migration_backfills_and_round_trips(
    db_session: AsyncSession,
) -> None:
    migration = _load_migration()
    connection = await db_session.connection()
    await connection.execute(
        text(
            """
            CREATE TEMPORARY TABLE agent_runs (
                id UUID PRIMARY KEY,
                started_at TIMESTAMPTZ NULL
            ) ON COMMIT DROP
            """
        )
    )
    await connection.execute(
        text(
            """
            INSERT INTO agent_runs (id, started_at)
            VALUES
              ('00000000-0000-4000-a000-000000000001', NULL),
              ('00000000-0000-4000-a000-000000000002', '2026-07-11T00:00:00+00:00')
            """
        )
    )

    await connection.execute(text("SET lock_timeout = '0'"))
    await _invoke_migration(connection, migration, "upgrade")
    rows = (
        await connection.execute(
            text("SELECT id, attempt_epoch FROM agent_runs ORDER BY id")
        )
    ).all()
    assert rows == [
        (uuid.UUID("00000000-0000-4000-a000-000000000001"), 0),
        (uuid.UUID("00000000-0000-4000-a000-000000000002"), 1),
    ]
    lock_timeout = (await connection.execute(text("SHOW lock_timeout"))).scalar_one()
    assert lock_timeout == "5s"
    column_contract = await _read_column_contract(connection)
    assert column_contract == ("bigint", True, "0")
    check_definition = await _read_check_definition(connection)
    assert check_definition == "CHECK ((attempt_epoch >= 0))"

    await connection.execute(text("SET lock_timeout = '0'"))
    await _invoke_migration(connection, migration, "downgrade")
    downgrade_lock_timeout = (
        await connection.execute(text("SHOW lock_timeout"))
    ).scalar_one()
    assert downgrade_lock_timeout == "5s"
    column_exists = (
        await connection.execute(
            text(
                """
                SELECT EXISTS (
                    SELECT 1
                    FROM pg_attribute
                    WHERE attrelid = 'agent_runs'::regclass
                      AND attname = 'attempt_epoch'
                      AND NOT attisdropped
                )
                """
            )
        )
    ).scalar_one()
    assert column_exists is False

    await connection.execute(text("SET lock_timeout = '0'"))
    await _invoke_migration(connection, migration, "upgrade")
    reupgrade_lock_timeout = (
        await connection.execute(text("SHOW lock_timeout"))
    ).scalar_one()
    assert reupgrade_lock_timeout == "5s"
    rows_after_reupgrade = (
        await connection.execute(
            text("SELECT id, attempt_epoch FROM agent_runs ORDER BY id")
        )
    ).all()
    assert rows_after_reupgrade == rows
    assert await _read_column_contract(connection) == column_contract
    assert await _read_check_definition(connection) == check_definition
