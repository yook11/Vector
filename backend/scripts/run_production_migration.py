"""本番pending rangeがexpand-onlyの場合だけ同一DB sessionで適用する。"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Callable, Sequence
from contextlib import AbstractContextManager
from typing import Any

from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from pydantic import ValidationError

from alembic import command
from app.migration_config import load_migration_settings
from app.migration_db import create_migration_engine
from app.migration_lock import MigrationLockUnavailable, migration_advisory_lock
from scripts.migration_gate import (
    ChangedMigrationDecision,
    MigrationGateError,
    decide_changed_migrations,
    pending_revision_paths,
)

EXIT_SUCCESS = 0
EXIT_INVALID = 2
EXIT_MANUAL_REQUIRED = 10
EXIT_LOCK_CONFLICT = 11
EXIT_MIGRATION_FAILED = 12


class MigrationRunnerContractError(RuntimeError):
    """本番runner固有の設定・実行契約違反。"""


def execute_production_migration(
    *,
    lock: AbstractContextManager[None],
    load_decision: Callable[[], ChangedMigrationDecision],
    upgrade: Callable[[], None],
    read_heads: Callable[[], tuple[Sequence[str], Sequence[str]]],
) -> int:
    """lock内で分類・適用・head確認を行い安定した終了コードへ写像する。"""
    try:
        with lock:
            db_heads, script_heads = read_heads()
            if len(db_heads) > 1 or len(script_heads) != 1:
                return EXIT_INVALID
            decision = load_decision()
            if decision.decision == "manual":
                return EXIT_MANUAL_REQUIRED
            if decision.decision == "invalid":
                return EXIT_INVALID
            upgrade()
            db_heads, script_heads = read_heads()
            if len(db_heads) != 1 or len(script_heads) != 1:
                return EXIT_MIGRATION_FAILED
            if db_heads[0] != script_heads[0]:
                return EXIT_MIGRATION_FAILED
            return EXIT_SUCCESS
    except MigrationLockUnavailable:
        return EXIT_LOCK_CONFLICT
    except MigrationGateError:
        return EXIT_INVALID
    except Exception:
        return EXIT_MIGRATION_FAILED


def _heads(
    connection: Any,
    script: ScriptDirectory,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    migration = MigrationContext.configure(connection)
    return tuple(migration.get_current_heads()), tuple(script.get_heads())


def _run_on_connection(connection: Any) -> int:
    alembic_config = Config("alembic.ini")
    script = ScriptDirectory.from_config(alembic_config)

    def read_heads() -> tuple[tuple[str, ...], tuple[str, ...]]:
        return _heads(connection, script)

    def load_decision() -> ChangedMigrationDecision:
        current_heads, _ = read_heads()
        paths = pending_revision_paths(script, current_heads)
        return decide_changed_migrations(paths)

    def upgrade() -> None:
        # introspection SELECTのtransactionを閉じ、AlembicにDDL transactionを委ねる。
        connection.commit()
        alembic_config.attributes["connection"] = connection
        command.upgrade(alembic_config, "head")

    return execute_production_migration(
        lock=migration_advisory_lock(connection),
        load_decision=load_decision,
        upgrade=upgrade,
        read_heads=read_heads,
    )


async def _run() -> int:
    settings = load_migration_settings()
    if settings.env != "production":
        raise MigrationRunnerContractError(
            "production migration runner requires ENV=production"
        )
    engine = create_migration_engine(settings)
    try:
        async with engine.connect() as connection:
            return await connection.run_sync(_run_on_connection)
    finally:
        await engine.dispose()


def main() -> int:
    try:
        result = asyncio.run(_run())
    except (MigrationRunnerContractError, ValidationError, ValueError) as exc:
        print(
            f"Production migration contract failed: {type(exc).__name__}",
            file=sys.stderr,
        )
        return EXIT_INVALID
    except Exception as exc:
        print(
            f"Production migration execution failed: {type(exc).__name__}",
            file=sys.stderr,
        )
        return EXIT_MIGRATION_FAILED
    if result == EXIT_SUCCESS:
        print("Production migration completed at the single Alembic head.")
    elif result == EXIT_MANUAL_REQUIRED:
        print("Pending migration range requires manual application.", file=sys.stderr)
    elif result == EXIT_LOCK_CONFLICT:
        print("Another online migration holds the advisory lock.", file=sys.stderr)
    elif result == EXIT_INVALID:
        print("Migration contract validation failed.", file=sys.stderr)
    else:
        print("Production migration failed.", file=sys.stderr)
    return result


if __name__ == "__main__":
    raise SystemExit(main())
