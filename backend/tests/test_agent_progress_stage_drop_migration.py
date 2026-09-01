"""progress_stage 列 drop migration の往復契約。"""

from __future__ import annotations

import importlib.util
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
    / "z17_drop_agent_run_progress_stage.py"
)


def _load_migration() -> ModuleType:
    assert MIGRATION_PATH.exists(), f"missing migration: {MIGRATION_PATH}"
    spec = importlib.util.spec_from_file_location(
        "test_z17_drop_agent_run_progress_stage",
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


async def _column_exists(connection: AsyncConnection) -> bool:
    return bool(
        await connection.scalar(
            text(
                """
                SELECT 1
                FROM pg_attribute
                WHERE attrelid = 'agent_runs'::regclass
                  AND attname = 'progress_stage'
                  AND NOT attisdropped
                """
            )
        )
    )


async def _check_exists(connection: AsyncConnection) -> bool:
    return bool(
        await connection.scalar(
            text(
                """
                SELECT 1
                FROM pg_constraint
                WHERE conrelid = 'agent_runs'::regclass
                  AND conname = 'ck_agent_runs_progress_stage'
                """
            )
        )
    )


@pytest.mark.asyncio
async def test_progress_stage_drop_round_trips_without_restoring_values(
    db_session: AsyncSession,
) -> None:
    migration = _load_migration()
    connection = await db_session.connection()
    await connection.execute(
        text(
            """
            CREATE TEMPORARY TABLE agent_runs (
                id UUID PRIMARY KEY,
                progress_stage VARCHAR(32) NULL,
                CONSTRAINT ck_agent_runs_progress_stage
                    CHECK (progress_stage IN (
                        'safety_check', 'context_resolution', 'planning',
                        'evidence_collection', 'evidence_review', 'answering'
                    ))
            ) ON COMMIT PRESERVE ROWS
            """
        )
    )
    await connection.execute(
        text(
            """
            INSERT INTO agent_runs (id, progress_stage)
            VALUES ('00000000-0000-4000-a000-000000000001', 'answering')
            """
        )
    )

    await connection.execute(text("SET lock_timeout = '0'"))
    await connection.execute(text("SET statement_timeout = '0'"))
    await _invoke_migration(connection, migration, "upgrade")

    assert await _column_exists(connection) is False
    assert await _check_exists(connection) is False

    await connection.execute(text("SET lock_timeout = '0'"))
    await connection.execute(text("SET statement_timeout = '0'"))
    await _invoke_migration(connection, migration, "downgrade")

    assert await _column_exists(connection) is True
    assert await _check_exists(connection) is True
    assert (
        await connection.scalar(
            text("SELECT progress_stage FROM agent_runs WHERE id = :id"),
            {"id": "00000000-0000-4000-a000-000000000001"},
        )
        is None
    )
    assert (
        await connection.scalar(
            text(
                """
            SELECT attnotnull
            FROM pg_attribute
            WHERE attrelid = 'agent_runs'::regclass
              AND attname = 'progress_stage'
              AND NOT attisdropped
            """
            )
        )
        is False
    )
