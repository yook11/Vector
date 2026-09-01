"""production migration runner の適用前判定と終了コード。"""

from __future__ import annotations

import sys
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from typing import NoReturn

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

import run_production_migration as production_migration  # noqa: E402
from migration_gate import ChangedMigrationDecision  # noqa: E402
from run_production_migration import (  # noqa: E402
    EXIT_INVALID,
    EXIT_LOCK_CONFLICT,
    EXIT_MANUAL_REQUIRED,
    EXIT_MIGRATION_FAILED,
    EXIT_SUCCESS,
    MigrationRunnerContractError,
    execute_production_migration,
)


def _decision(value: str) -> ChangedMigrationDecision:
    return ChangedMigrationDecision(decision=value, revisions=())  # type: ignore[arg-type]


@pytest.mark.parametrize("decision", ["none", "expand"])
def test_empty_or_expand_pending_range_is_applied(decision: str) -> None:
    upgraded: list[bool] = []
    result = execute_production_migration(
        lock=nullcontext(),
        load_decision=lambda: _decision(decision),
        upgrade=lambda: upgraded.append(True),
        read_heads=lambda: (("z16",), ("z16",)),
    )
    assert (result, upgraded) == (EXIT_SUCCESS, [True])


def test_contract_pending_range_is_not_applied() -> None:
    upgraded: list[bool] = []
    result = execute_production_migration(
        lock=nullcontext(),
        load_decision=lambda: _decision("manual"),
        upgrade=lambda: upgraded.append(True),
        read_heads=lambda: (("z16",), ("z16",)),
    )
    assert (result, upgraded) == (EXIT_MANUAL_REQUIRED, [])


def test_invalid_pending_range_is_not_applied() -> None:
    result = execute_production_migration(
        lock=nullcontext(),
        load_decision=lambda: _decision("invalid"),
        upgrade=lambda: None,
        read_heads=lambda: (("z16",), ("z16",)),
    )
    assert result == EXIT_INVALID


def test_multiple_heads_are_invalid() -> None:
    result = execute_production_migration(
        lock=nullcontext(),
        load_decision=lambda: _decision("none"),
        upgrade=lambda: None,
        read_heads=lambda: (("z16", "branch"), ("z16",)),
    )
    assert result == EXIT_INVALID


def test_lock_conflict_has_distinct_exit_code() -> None:
    from app.migration_lock import MigrationLockUnavailable

    class _Conflict:
        def __enter__(self) -> None:
            raise MigrationLockUnavailable

        def __exit__(self, *_args: object) -> None:
            return None

    result = execute_production_migration(
        lock=_Conflict(),
        load_decision=lambda: _decision("expand"),
        upgrade=lambda: None,
        read_heads=lambda: (("z16",), ("z16",)),
    )
    assert result == EXIT_LOCK_CONFLICT


def test_upgrade_failure_has_distinct_exit_code() -> None:
    def fail() -> None:
        raise RuntimeError("database rejected DDL")

    result = execute_production_migration(
        lock=nullcontext(),
        load_decision=lambda: _decision("expand"),
        upgrade=fail,
        read_heads=lambda: (("z16",), ("z16",)),
    )
    assert result == EXIT_MIGRATION_FAILED


def test_head_mismatch_after_upgrade_fails() -> None:
    result = execute_production_migration(
        lock=nullcontext(),
        load_decision=lambda: _decision("expand"),
        upgrade=lambda: None,
        read_heads=lambda: (("z15",), ("z16",)),
    )
    assert result == EXIT_MIGRATION_FAILED


@pytest.mark.asyncio
async def test_production_runner_rejects_non_production_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        production_migration,
        "load_migration_settings",
        lambda: SimpleNamespace(env="development"),
    )

    with pytest.raises(MigrationRunnerContractError):
        await production_migration._run()


def test_main_maps_database_connection_failure_to_migration_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fail() -> NoReturn:
        raise ConnectionError("database unavailable")

    monkeypatch.setattr(production_migration, "_run", fail)
    assert production_migration.main() == EXIT_MIGRATION_FAILED


def test_main_maps_runner_contract_failure_to_invalid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fail() -> NoReturn:
        raise MigrationRunnerContractError("ENV is not production")

    monkeypatch.setattr(production_migration, "_run", fail)
    assert production_migration.main() == EXIT_INVALID


def test_connection_lock_classification_upgrade_and_verification_share_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    connection = SimpleNamespace(commit=lambda: events.append("commit"))
    config = SimpleNamespace(attributes={})
    script = object()

    class Lock:
        def __enter__(self) -> None:
            events.append("lock-enter")

        def __exit__(self, *_args: object) -> None:
            events.append("lock-exit")

    monkeypatch.setattr(production_migration, "Config", lambda _path: config)
    monkeypatch.setattr(
        production_migration.ScriptDirectory,
        "from_config",
        lambda _config: script,
    )
    monkeypatch.setattr(
        production_migration,
        "_heads",
        lambda actual_connection, actual_script: (
            events.append("heads"),
            (assert_connection(actual_connection, connection),),
            (assert_connection(actual_script, script),),
        )[1:],
    )
    monkeypatch.setattr(
        production_migration,
        "pending_revision_paths",
        lambda actual_script, _heads: (
            events.append("classify"),
            assert_connection(actual_script, script),
            [],
        )[2],
    )
    monkeypatch.setattr(
        production_migration,
        "migration_advisory_lock",
        lambda actual_connection: (
            events.append("lock-created"),
            assert_connection(actual_connection, connection),
            Lock(),
        )[2],
    )

    def upgrade(actual_config: object, revision: str) -> None:
        assert actual_config is config
        assert revision == "head"
        assert config.attributes == {"connection": connection}
        events.append("upgrade")

    monkeypatch.setattr(production_migration.command, "upgrade", upgrade)
    result = production_migration._run_on_connection(connection)

    assert result == EXIT_SUCCESS
    assert events == [
        "lock-created",
        "lock-enter",
        "heads",
        "heads",
        "classify",
        "commit",
        "upgrade",
        "heads",
        "lock-exit",
    ]


def assert_connection(actual: object, expected: object) -> str:
    assert actual is expected
    return "z16"
