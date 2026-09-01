"""online Alembic 全経路で共有する advisory lock の契約。"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pytest

from app.migration_lock import MigrationLockUnavailable, migration_advisory_lock

_BACKEND_ROOT = Path(__file__).resolve().parents[1]


@dataclass
class _Result:
    value: bool

    def scalar_one(self) -> bool:
        return self.value


@dataclass
class _Connection:
    acquire: bool = True
    calls: list[str] = field(default_factory=list)

    def execute(self, statement: object, _parameters: object) -> _Result:
        sql = str(statement)
        self.calls.append(sql)
        return _Result(self.acquire if "try_advisory" in sql else True)


def test_advisory_lock_conflict_fails_without_entering() -> None:
    connection = _Connection(acquire=False)
    with pytest.raises(MigrationLockUnavailable):
        with migration_advisory_lock(connection):
            pytest.fail("lock conflict must not enter the protected section")


def test_advisory_lock_is_released_after_failure() -> None:
    connection = _Connection()
    with pytest.raises(RuntimeError, match="upgrade failed"):
        with migration_advisory_lock(connection):
            raise RuntimeError("upgrade failed")
    assert "pg_advisory_unlock" in connection.calls[-1]


def test_alembic_env_has_no_caller_owned_lock_bypass() -> None:
    env_source = (_BACKEND_ROOT / "alembic" / "env.py").read_text(encoding="utf-8")

    assert "migration_lock_owned" not in env_source
