"""policy_blocked status migration の往復契約。"""

from __future__ import annotations

import importlib.util
import re
from io import StringIO
from pathlib import Path
from types import ModuleType

import pytest
from alembic.operations import Operations
from alembic.runtime.migration import MigrationContext
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

_VERSIONS_DIR = Path(__file__).parents[1] / "alembic" / "versions"


def _load_migration() -> ModuleType:
    paths = sorted(_VERSIONS_DIR.glob("*policy_blocked*.py"))
    assert len(paths) == 1, "missing policy_blocked status migration"
    spec = importlib.util.spec_from_file_location(
        "test_y5_policy_blocked_agent_run_status",
        paths[0],
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _render_offline_sql(migration: ModuleType, operation: str) -> str:
    output = StringIO()
    context = MigrationContext.configure(
        url="postgresql://",
        opts={"as_sql": True, "output_buffer": output},
    )
    migration.op = Operations(context)

    getattr(migration, operation)()

    return output.getvalue()


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


async def _assert_invalid_policy_row(
    connection: AsyncConnection,
    *,
    assistant_message_id: str | None,
    error_code: str | None,
) -> None:
    with pytest.raises(IntegrityError):
        async with connection.begin_nested():
            await connection.execute(
                text(
                    """
                    INSERT INTO agent_runs (
                        id,
                        status,
                        assistant_message_id,
                        error_code
                    )
                    VALUES (
                        :id,
                        'policy_blocked',
                        CAST(:assistant_message_id AS uuid),
                        :error_code
                    )
                    """
                ),
                {
                    "id": (
                        "00000000-0000-4000-a000-000000000006"
                        if assistant_message_id is not None
                        else "00000000-0000-4000-a000-000000000007"
                    ),
                    "assistant_message_id": assistant_message_id,
                    "error_code": error_code,
                },
            )


async def _status_check_definition(connection: AsyncConnection) -> str:
    return (
        await connection.execute(
            text(
                """
                SELECT pg_get_constraintdef(oid)
                FROM pg_constraint
                WHERE conrelid = 'agent_runs'::regclass
                  AND conname = 'ck_agent_runs_status'
                """
            )
        )
    ).scalar_one()


@pytest.mark.asyncio
async def test_policy_blocked_migration_preserves_rows_and_refuses_lossy_downgrade(
    db_session: AsyncSession,
) -> None:
    migration = _load_migration()
    connection = await db_session.connection()
    await connection.execute(
        text(
            """
            CREATE TEMPORARY TABLE agent_runs (
                id UUID PRIMARY KEY,
                status VARCHAR(32) NOT NULL,
                assistant_message_id UUID NULL,
                error_code TEXT NULL,
                CONSTRAINT ck_agent_runs_status
                    CHECK (status IN ('queued', 'running', 'completed', 'failed')),
                CONSTRAINT ck_agent_runs_completed_answer
                    CHECK ((status = 'completed') = (assistant_message_id IS NOT NULL)),
                CONSTRAINT ck_agent_runs_failed_error
                    CHECK ((status = 'failed') = (error_code IS NOT NULL))
            ) ON COMMIT PRESERVE ROWS
            """
        )
    )
    await connection.execute(
        text(
            """
            INSERT INTO agent_runs (id, status, assistant_message_id, error_code)
            VALUES
              ('00000000-0000-4000-a000-000000000001', 'queued', NULL, NULL),
              ('00000000-0000-4000-a000-000000000002', 'running', NULL, NULL),
              (
                '00000000-0000-4000-a000-000000000003',
                'completed',
                '00000000-0000-4000-a000-000000000004',
                NULL
              ),
              ('00000000-0000-4000-a000-000000000005', 'failed', NULL, 'internal_error')
            """
        )
    )

    assert migration.down_revision == "y4_agent_user_daily_quotas"
    await _invoke_migration(connection, migration, "upgrade")
    await connection.execute(
        text(
            """
            INSERT INTO agent_runs (
                id,
                status,
                assistant_message_id,
                error_code
            )
            VALUES (
                '00000000-0000-4000-a000-000000000008',
                'policy_blocked',
                NULL,
                NULL
            )
            """
        )
    )
    rows = (
        await connection.execute(
            text("SELECT id::text, status FROM agent_runs ORDER BY id")
        )
    ).all()

    assert rows == [
        ("00000000-0000-4000-a000-000000000001", "queued"),
        ("00000000-0000-4000-a000-000000000002", "running"),
        ("00000000-0000-4000-a000-000000000003", "completed"),
        ("00000000-0000-4000-a000-000000000005", "failed"),
        ("00000000-0000-4000-a000-000000000008", "policy_blocked"),
    ]
    await _assert_invalid_policy_row(
        connection,
        assistant_message_id="00000000-0000-4000-a000-000000000009",
        error_code=None,
    )
    await _assert_invalid_policy_row(
        connection,
        assistant_message_id=None,
        error_code="internal_error",
    )

    with pytest.raises(Exception, match="policy_blocked"):
        await _invoke_migration(connection, migration, "downgrade")
    policy_status = await connection.scalar(
        text(
            """
            SELECT status FROM agent_runs
            WHERE id = '00000000-0000-4000-a000-000000000008'
            """
        )
    )

    assert policy_status == "policy_blocked"

    await connection.execute(
        text(
            """
            DELETE FROM agent_runs
            WHERE id = '00000000-0000-4000-a000-000000000008'
            """
        )
    )
    await _invoke_migration(connection, migration, "downgrade")
    status_check = await _status_check_definition(connection)

    assert "policy_blocked" not in status_check
    assert all(
        status in status_check
        for status in ("queued", "running", "completed", "failed")
    )
    await _assert_invalid_policy_row(
        connection,
        assistant_message_id=None,
        error_code=None,
    )


def test_policy_blocked_offline_downgrade_emits_fail_closed_guard() -> None:
    sql = _render_offline_sql(_load_migration(), "downgrade")
    normalized = " ".join(sql.split()).upper()

    assert "DO $$" in normalized
    assert "IF EXISTS" in normalized
    assert "WHERE STATUS = 'POLICY_BLOCKED'" in normalized
    assert "RAISE EXCEPTION" in normalized


@pytest.mark.parametrize("operation", ["upgrade", "downgrade"])
def test_policy_blocked_migration_resets_timeouts_after_its_ddl(
    operation: str,
) -> None:
    sql = _render_offline_sql(_load_migration(), operation)
    normalized = " ".join(sql.split()).upper()
    last_ddl_position = normalized.rfind("ALTER TABLE")
    reset_positions = {
        "statement_timeout": normalized.rfind("RESET STATEMENT_TIMEOUT;"),
        "lock_timeout": normalized.rfind("RESET LOCK_TIMEOUT;"),
    }

    assert last_ddl_position >= 0
    assert all(position > last_ddl_position for position in reset_positions.values())
    assert normalized[max(reset_positions.values()) :] in {
        "RESET STATEMENT_TIMEOUT;",
        "RESET LOCK_TIMEOUT;",
    }


@pytest.mark.parametrize("operation", ["upgrade", "downgrade"])
def test_policy_blocked_migration_bounds_lock_and_statement_time_before_ddl(
    operation: str,
) -> None:
    sql = _render_offline_sql(_load_migration(), operation)
    lock_timeout = re.search(
        r"SET(?: LOCAL)? lock_timeout\s*=\s*'([^']+)'",
        sql,
        flags=re.IGNORECASE,
    )
    statement_timeout = re.search(
        r"SET(?: LOCAL)? statement_timeout\s*=\s*'([^']+)'",
        sql,
        flags=re.IGNORECASE,
    )
    ddl_position = sql.upper().find("ALTER TABLE")

    assert lock_timeout is not None
    assert statement_timeout is not None
    assert lock_timeout.group(1).lower() not in {"0", "0ms", "0s", "0min"}
    assert statement_timeout.group(1).lower() not in {"0", "0ms", "0s", "0min"}
    assert ddl_position > max(lock_timeout.start(), statement_timeout.start())
