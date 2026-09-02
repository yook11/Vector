"""run時刻列の削除と、値を復元しないdowngradeの契約。"""

from __future__ import annotations

import importlib.util
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType

import pytest
from alembic.operations import Operations
from alembic.runtime.migration import MigrationContext
from sqlalchemy import Connection, text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

MIGRATION_PATH = (
    Path(__file__).parents[1] / "alembic" / "versions" / "z19_drop_run_time_columns.py"
)
_REMOVED_COLUMNS = {"started_at", "completed_at"}


@dataclass(frozen=True)
class _ColumnContract:
    data_type: str
    nullable: bool
    default: str | None


def _load_migration() -> ModuleType:
    assert MIGRATION_PATH.exists(), f"missing migration: {MIGRATION_PATH}"
    spec = importlib.util.spec_from_file_location(
        "test_z19_drop_run_time_columns", MIGRATION_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def _invoke_migration(
    connection: AsyncConnection, migration: ModuleType, operation: str
) -> None:
    def invoke(sync_connection: Connection) -> None:
        migration.op = Operations(MigrationContext.configure(sync_connection))
        getattr(migration, operation)()

    await connection.run_sync(invoke)


async def _read_columns(connection: AsyncConnection) -> dict[str, _ColumnContract]:
    rows = (
        await connection.execute(
            text(
                """
                SELECT attribute.attname AS name,
                       format_type(attribute.atttypid, attribute.atttypmod)
                           AS data_type,
                       NOT attribute.attnotnull AS nullable,
                       pg_get_expr(default_value.adbin, default_value.adrelid)
                           AS column_default
                FROM pg_attribute AS attribute
                LEFT JOIN pg_attrdef AS default_value
                  ON default_value.adrelid = attribute.attrelid
                 AND default_value.adnum = attribute.attnum
                WHERE attribute.attrelid = 'pg_temp.agent_runs'::regclass
                  AND attribute.attnum > 0
                  AND NOT attribute.attisdropped
                """
            )
        )
    ).all()
    return {
        row.name: _ColumnContract(
            data_type=row.data_type,
            nullable=row.nullable,
            default=row.column_default,
        )
        for row in rows
    }


async def _read_retained_runs(
    connection: AsyncConnection,
) -> list[dict[str, object]]:
    return list(
        (
            await connection.execute(
                text(
                    """
                    SELECT to_jsonb(run) - ARRAY['started_at', 'completed_at']
                    FROM pg_temp.agent_runs AS run
                    ORDER BY run.id
                    """
                )
            )
        ).scalars()
    )


@pytest.mark.asyncio
async def test_run_time_columns_drop_round_trips_without_restoring_values(
    db_session: AsyncSession,
) -> None:
    """往復後も他の列とデータを維持し、削除した開始・終端時刻だけは復元しない。"""
    migration = _load_migration()
    connection = await db_session.connection()
    # 現行ORMから作ると旧列の欠落を見逃すため、z18時点の列を独立に用意する。
    await connection.execute(
        text(
            """
            CREATE TEMPORARY TABLE agent_runs (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                thread_id UUID NOT NULL,
                user_message_id UUID NOT NULL,
                assistant_message_id UUID NULL,
                status VARCHAR(32) NOT NULL,
                error_code TEXT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                started_at TIMESTAMPTZ NULL,
                completed_at TIMESTAMPTZ NULL,
                attempt_epoch BIGINT NOT NULL DEFAULT 0,
                quota_usage_date DATE NULL,
                research_checkpoint JSONB NULL,
                deadline_at TIMESTAMPTZ NOT NULL
            ) ON COMMIT DROP
            """
        )
    )
    await connection.execute(
        text(
            """
            INSERT INTO pg_temp.agent_runs (
                id, thread_id, user_message_id, assistant_message_id,
                status, error_code, created_at, started_at, completed_at,
                attempt_epoch, quota_usage_date, research_checkpoint, deadline_at
            ) VALUES (
                '00000000-0000-4000-a000-000000000001',
                '00000000-0000-4000-a000-000000000011',
                '00000000-0000-4000-a000-000000000021',
                '00000000-0000-4000-a000-000000000031',
                'completed', NULL, '2026-09-02T00:00:00Z',
                '2026-09-02T00:00:01Z', '2026-09-02T00:00:10Z',
                2, '2026-09-02', '{"legacy": "preserve"}',
                '2026-09-02T00:01:00Z'
            ), (
                '00000000-0000-4000-a000-000000000002',
                '00000000-0000-4000-a000-000000000012',
                '00000000-0000-4000-a000-000000000022', NULL,
                'queued', NULL, '2026-09-02T01:00:00Z', NULL, NULL,
                0, NULL, NULL, '2026-09-02T01:01:00Z'
            )
            """
        )
    )
    original_columns = await _read_columns(connection)
    retained_columns = {
        name: contract
        for name, contract in original_columns.items()
        if name not in _REMOVED_COLUMNS
    }
    retained_runs = await _read_retained_runs(connection)

    await _invoke_migration(connection, migration, "upgrade")

    assert await _read_columns(connection) == retained_columns
    assert await _read_retained_runs(connection) == retained_runs

    await _invoke_migration(connection, migration, "downgrade")

    restored_columns = await _read_columns(connection)
    assert restored_columns.keys() == original_columns.keys()
    for name, expected in retained_columns.items():
        assert restored_columns[name] == expected
    for name in _REMOVED_COLUMNS:
        restored_column = restored_columns[name]
        assert restored_column.data_type == "timestamp with time zone"
        assert restored_column.nullable is True
        assert restored_column.default is None
    persisted_runs = (
        await connection.execute(
            text("SELECT started_at, completed_at FROM pg_temp.agent_runs ORDER BY id")
        )
    ).all()
    for persisted in persisted_runs:
        assert persisted.started_at is None
        assert persisted.completed_at is None
    assert await _read_retained_runs(connection) == retained_runs

    await _invoke_migration(connection, migration, "upgrade")

    assert await _read_columns(connection) == retained_columns
    assert await _read_retained_runs(connection) == retained_runs
