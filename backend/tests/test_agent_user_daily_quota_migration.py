"""日次quota schema migrationの永続契約。"""

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
    Path(__file__).parents[1] / "alembic" / "versions" / "y4_agent_user_daily_quotas.py"
)


def _load_migration() -> ModuleType:
    assert MIGRATION_PATH.exists(), f"missing migration: {MIGRATION_PATH}"
    spec = importlib.util.spec_from_file_location(
        "test_y4_agent_user_daily_quotas",
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


async def _read_column_contract(
    connection: AsyncConnection,
    *,
    table_name: str,
    column_name: str,
) -> tuple[str, bool, str | None]:
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
                WHERE attribute.attrelid = CAST(:table_name AS regclass)
                  AND attribute.attname = :column_name
                  AND NOT attribute.attisdropped
                """
            ),
            {"table_name": table_name, "column_name": column_name},
        )
    ).one()


async def _read_quota_constraint_definitions(
    connection: AsyncConnection,
) -> list[str]:
    return list(
        (
            await connection.execute(
                text(
                    """
                    SELECT pg_get_constraintdef(oid)
                    FROM pg_constraint
                    WHERE conrelid = 'public.agent_user_daily_quotas'::regclass
                    ORDER BY contype, conname
                    """
                )
            )
        ).scalars()
    )


async def _has_runtime_dml_grant(connection: AsyncConnection) -> bool:
    return bool(
        (
            await connection.execute(
                text(
                    """
                    SELECT has_table_privilege(
                             'vector_app',
                             'public.agent_user_daily_quotas',
                             'SELECT'
                           )
                       AND has_table_privilege(
                             'vector_app',
                             'public.agent_user_daily_quotas',
                             'INSERT'
                           )
                       AND has_table_privilege(
                             'vector_app',
                             'public.agent_user_daily_quotas',
                             'UPDATE'
                           )
                       AND has_table_privilege(
                             'vector_app',
                             'public.agent_user_daily_quotas',
                             'DELETE'
                           )
                    """
                )
            )
        ).scalar_one()
    )


async def _quota_table_exists(connection: AsyncConnection) -> bool:
    return bool(
        (
            await connection.execute(
                text("SELECT to_regclass('public.agent_user_daily_quotas') IS NOT NULL")
            )
        ).scalar_one()
    )


def _compact_sql(definition: str) -> str:
    return "".join(definition.lower().split())


@pytest.mark.asyncio
async def test_daily_quota_migration_preserves_legacy_runs_and_round_trips(
    db_session: AsyncSession,
) -> None:
    migration = _load_migration()
    connection = await db_session.connection()
    # setup_db のmetadata作成をy3時点へ戻し、unqualifiedなmigration DDLを検証する。
    assert await _quota_table_exists(connection) is True
    await connection.execute(text("DROP TABLE public.agent_user_daily_quotas"))
    assert await _quota_table_exists(connection) is False
    await connection.execute(
        text(
            """
            CREATE TEMPORARY TABLE agent_runs (
                id UUID PRIMARY KEY,
                attempt_epoch BIGINT NOT NULL DEFAULT 0
            ) ON COMMIT DROP
            """
        )
    )
    await connection.execute(
        text(
            """
            INSERT INTO agent_runs (id)
            VALUES
              ('00000000-0000-4000-a000-000000000001'),
              ('00000000-0000-4000-a000-000000000002')
            """
        )
    )

    assert migration.revision == "y4_agent_user_daily_quotas"
    assert migration.down_revision == "y3_agent_runs_attempt_epoch"
    assert migration.MIGRATION_KIND == "contract"

    await _invoke_migration(connection, migration, "upgrade")
    quota_columns = [
        await _read_column_contract(
            connection,
            table_name="public.agent_user_daily_quotas",
            column_name=column_name,
        )
        for column_name in ("user_id", "usage_date", "used_count")
    ]
    quota_constraints = await _read_quota_constraint_definitions(connection)
    quota_constraint_sql = [
        _compact_sql(definition) for definition in quota_constraints
    ]
    legacy_rows = (
        await connection.execute(
            text("SELECT id, quota_usage_date FROM agent_runs ORDER BY id")
        )
    ).all()

    assert quota_columns == [
        ("uuid", True, None),
        ("date", True, None),
        ("integer", True, None),
    ]
    assert "PRIMARY KEY (user_id, usage_date)" in quota_constraints
    assert (
        'FOREIGN KEY (user_id) REFERENCES auth."user"(id) ON DELETE CASCADE'
        in quota_constraints
    )
    assert any(
        "check(" in definition
        and "used_count" in definition
        and ">=0" in definition
        and "<=10" in definition
        for definition in quota_constraint_sql
    )
    assert await _read_column_contract(
        connection,
        table_name="agent_runs",
        column_name="quota_usage_date",
    ) == ("date", False, None)
    assert legacy_rows == [
        (uuid.UUID("00000000-0000-4000-a000-000000000001"), None),
        (uuid.UUID("00000000-0000-4000-a000-000000000002"), None),
    ]
    assert await _has_runtime_dml_grant(connection) is True

    await _invoke_migration(connection, migration, "downgrade")

    assert await _quota_table_exists(connection) is False
    quota_usage_date_exists = (
        await connection.execute(
            text(
                """
                SELECT EXISTS (
                    SELECT 1
                    FROM pg_attribute
                    WHERE attrelid = 'agent_runs'::regclass
                      AND attname = 'quota_usage_date'
                      AND NOT attisdropped
                )
                """
            )
        )
    ).scalar_one()
    assert quota_usage_date_exists is False

    await _invoke_migration(connection, migration, "upgrade")

    assert await _quota_table_exists(connection) is True
    assert await _has_runtime_dml_grant(connection) is True
    assert (
        await connection.execute(
            text("SELECT id, quota_usage_date FROM agent_runs ORDER BY id")
        )
    ).all() == legacy_rows
