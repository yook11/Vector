"""online migrationを直列化するPostgreSQL session advisory lock。"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from sqlalchemy import text

# repository固有の固定値で、他application advisory lockと衝突させない。
_MIGRATION_LOCK_KEY = 0x564543544F52


class MigrationLockUnavailable(RuntimeError):
    """別sessionがonline migrationを実行中。"""


@contextmanager
def migration_advisory_lock(connection: Any) -> Iterator[None]:
    """競合時に待たず失敗し、同じconnectionが生きている間lockを保持する。"""
    acquired = connection.execute(
        text("SELECT pg_try_advisory_lock(:key)"),
        {"key": _MIGRATION_LOCK_KEY},
    ).scalar_one()
    if not acquired:
        raise MigrationLockUnavailable("another online migration is running")
    if callable(commit := getattr(connection, "commit", None)):
        commit()
    try:
        yield
    finally:
        connection.execute(
            text("SELECT pg_advisory_unlock(:key)"),
            {"key": _MIGRATION_LOCK_KEY},
        )
        if callable(commit := getattr(connection, "commit", None)):
            commit()
