"""回答生成開始時刻を追加するexpand migrationの契約。"""

from __future__ import annotations

import importlib.util
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from types import ModuleType

import pytest
from alembic.operations import Operations
from alembic.runtime.migration import MigrationContext
from sqlalchemy import Connection, DateTime, text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

from app.models.agent_run import AgentRun

MIGRATION_PATH = (
    Path(__file__).parents[1]
    / "alembic"
    / "versions"
    / "z20_agent_run_answer_started_at.py"
)


@dataclass(frozen=True)
class _ColumnContract:
    data_type: str
    nullable: bool
    default: str | None


def _load_migration() -> ModuleType:
    assert MIGRATION_PATH.exists(), f"missing migration: {MIGRATION_PATH}"
    spec = importlib.util.spec_from_file_location(
        "test_z20_agent_run_answer_started_at",
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


async def _answer_started_at_has_schema_objects(
    connection: AsyncConnection,
) -> tuple[bool, bool]:
    return (
        await connection.execute(
            text(
                """
                SELECT EXISTS (
                           SELECT 1
                           FROM pg_constraint AS constraint_def
                           JOIN pg_attribute AS attribute
                             ON attribute.attrelid = constraint_def.conrelid
                            AND attribute.attnum = ANY (constraint_def.conkey)
                           WHERE constraint_def.conrelid =
                                 'pg_temp.agent_runs'::regclass
                             AND attribute.attname = 'answer_started_at'
                       ) AS has_constraint,
                       EXISTS (
                           SELECT 1
                           FROM pg_index AS index_def
                           JOIN pg_attribute AS attribute
                             ON attribute.attrelid = index_def.indrelid
                            AND attribute.attnum = ANY (index_def.indkey)
                           WHERE index_def.indrelid = 'pg_temp.agent_runs'::regclass
                             AND attribute.attname = 'answer_started_at'
                       ) AS has_index
                """
            )
        )
    ).one()


def test_answer_started_at_model_contract() -> None:
    column = AgentRun.__table__.c.answer_started_at

    assert isinstance(column.type, DateTime)
    assert column.type.timezone is True
    assert column.type.python_type is datetime
    assert column.nullable is True
    assert column.server_default is None
    assert not any(
        constraint.columns.contains_column(column)
        for constraint in column.table.constraints
    )
    assert not any(
        index.columns.contains_column(column) for index in column.table.indexes
    )


@pytest.mark.asyncio
async def test_answer_started_at_migration_preserves_runs_and_round_trips(
    db_session: AsyncSession,
) -> None:
    migration = _load_migration()
    connection = await db_session.connection()
    # 現行ORMではなく、migration直前のz19時点のschemaを独立に用意する。
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
                deadline_at TIMESTAMPTZ NOT NULL,
                attempt_epoch BIGINT NOT NULL DEFAULT 0,
                quota_usage_date DATE NULL,
                research_checkpoint JSONB NULL
            ) ON COMMIT DROP
            """
        )
    )
    await connection.execute(
        text(
            """
            INSERT INTO pg_temp.agent_runs (
                id, thread_id, user_message_id, assistant_message_id,
                status, error_code, created_at, deadline_at, attempt_epoch,
                quota_usage_date, research_checkpoint
            ) VALUES (
                '00000000-0000-4000-a000-000000000001',
                '00000000-0000-4000-a000-000000000011',
                '00000000-0000-4000-a000-000000000021', NULL,
                'running', NULL, '2026-09-05T00:00:00Z',
                '2026-09-05T00:01:00Z', 2, '2026-09-05',
                '{"handoff": "preserve"}'
            )
            """
        )
    )
    columns_before_upgrade = await _read_columns(connection)
    run_before_upgrade = (
        await connection.execute(
            text("SELECT to_jsonb(run) FROM pg_temp.agent_runs AS run")
        )
    ).scalar_one()

    assert migration.revision == "z20_agent_run_answer_started_at"
    assert migration.down_revision == "z19_drop_run_time_columns"
    assert migration.MIGRATION_KIND == "expand"

    await _invoke_migration(connection, migration, "upgrade")

    columns_after_upgrade = await _read_columns(connection)
    answer_started_at = columns_after_upgrade.pop("answer_started_at")
    run_after_upgrade = (
        await connection.execute(
            text(
                """
                SELECT to_jsonb(run) - 'answer_started_at', answer_started_at
                FROM pg_temp.agent_runs AS run
                """
            )
        )
    ).one()
    assert answer_started_at == _ColumnContract(
        data_type="timestamp with time zone",
        nullable=True,
        default=None,
    )
    assert columns_after_upgrade == columns_before_upgrade
    assert run_after_upgrade[0] == run_before_upgrade
    assert run_after_upgrade.answer_started_at is None
    assert await _answer_started_at_has_schema_objects(connection) == (False, False)
    assert (await connection.execute(text("SHOW lock_timeout"))).scalar_one() == "5s"
    assert (
        await connection.execute(text("SHOW statement_timeout"))
    ).scalar_one() == "5s"

    recorded_at = datetime.fromisoformat("2026-09-05T00:00:30+00:00")
    await connection.execute(
        text(
            """
            UPDATE pg_temp.agent_runs
            SET answer_started_at = :recorded_at
            """
        ),
        {"recorded_at": recorded_at},
    )
    await _invoke_migration(connection, migration, "downgrade")

    assert await _read_columns(connection) == columns_before_upgrade
    run_after_downgrade = (
        await connection.execute(
            text("SELECT to_jsonb(run) FROM pg_temp.agent_runs AS run")
        )
    ).scalar_one()
    assert run_after_downgrade == run_before_upgrade

    await _invoke_migration(connection, migration, "upgrade")

    columns_after_reupgrade = await _read_columns(connection)
    assert columns_after_reupgrade.pop("answer_started_at") == answer_started_at
    assert columns_after_reupgrade == columns_before_upgrade
    answer_started_at_after_reupgrade = (
        await connection.execute(
            text("SELECT answer_started_at FROM pg_temp.agent_runs")
        )
    ).scalar_one()
    assert answer_started_at_after_reupgrade is None
    assert await _answer_started_at_has_schema_objects(connection) == (False, False)
