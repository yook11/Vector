"""migration runner のprotocol・mode・適用境界。"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Literal

import pytest
from pydantic import ValidationError

from app.migration_config import (
    MigrationRunnerRequest,
    load_migration_runner_request,
)
from app.migration_lock import MigrationLockUnavailable
from scripts import migration_runner as runner
from scripts.migration_gate import ChangedMigrationDecision
from scripts.migration_runner import (
    EXIT_INVALID,
    EXIT_LOCK_CONFLICT,
    EXIT_MIGRATION_FAILED,
    EXIT_NO_CHANGES,
    EXIT_SUCCESS,
    PROTOCOL_VERSION,
    PendingMigrationRange,
    execute_migration,
    main,
)

pytestmark = pytest.mark.unit

_TREE_OID = "a" * 40


def _request(
    mode: Literal["expand", "contract", "verify"],
    *,
    expected_start_revision: str | None = "r1",
    target_revision: str = "r2",
) -> MigrationRunnerRequest:
    return MigrationRunnerRequest(
        protocol_version=PROTOCOL_VERSION,
        mode=mode,
        expected_start_revision=expected_start_revision,
        target_revision=target_revision,
        migration_tree_oid=_TREE_OID,
    )


def _pending(revisions: tuple[str, ...], decision: str) -> PendingMigrationRange:
    return PendingMigrationRange(
        revision_ids=revisions,
        decision=ChangedMigrationDecision(  # type: ignore[arg-type]
            decision=decision,
            revisions=(),
        ),
    )


@contextmanager
def _lock(events: list[str]) -> Iterator[None]:
    events.append("lock-enter")
    try:
        yield
    finally:
        events.append("lock-exit")


@pytest.mark.parametrize(
    ("mode", "decision"),
    [("expand", "expand"), ("contract", "manual")],
)
def test_matching_mode_applies_only_the_classified_pending_range(
    mode: Literal["expand", "contract", "verify"],
    decision: str,
) -> None:
    events: list[str] = []
    heads = iter([(("r1",), ("r2",)), (("r2",), ("r2",))])

    result = execute_migration(
        _request(mode),
        lock=_lock(events),
        read_heads=lambda: (events.append("heads"), next(heads))[1],
        load_pending=lambda: _pending(("r2",), decision),
        upgrade=lambda target: events.append(f"upgrade:{target}"),
        rollback=lambda: events.append("rollback"),
    )

    assert (
        result.protocol_version,
        result.mode,
        result.result,
        result.start_revision,
        result.end_revision,
        result.target_revision,
        result.pending_revisions,
        result.migration_tree_oid,
        result.exit_code,
    ) == (1, mode, "applied", "r1", "r2", "r2", ("r2",), _TREE_OID, EXIT_SUCCESS)
    assert events == [
        "lock-enter",
        "heads",
        "upgrade:r2",
        "heads",
        "lock-exit",
    ]


@pytest.mark.parametrize(
    ("mode", "decision"),
    [
        ("expand", "manual"),
        ("contract", "expand"),
        ("expand", "invalid"),
        ("contract", "invalid"),
    ],
)
def test_mode_mismatch_or_mixed_range_is_rejected_before_upgrade(
    mode: Literal["expand", "contract", "verify"],
    decision: str,
) -> None:
    upgraded: list[str] = []

    result = execute_migration(
        _request(mode),
        lock=_lock([]),
        read_heads=lambda: (("r1",), ("r2",)),
        load_pending=lambda: _pending(("r2",), decision),
        upgrade=upgraded.append,
        rollback=lambda: None,
    )

    assert (result.result, result.exit_code, upgraded) == ("rejected", EXIT_INVALID, [])


def test_verify_reads_matching_head_without_applying_migrations() -> None:
    events: list[str] = []

    result = execute_migration(
        _request("verify", expected_start_revision=None),
        lock=_lock(events),
        read_heads=lambda: (("r2",), ("r2",)),
        load_pending=lambda: _pending((), "none"),
        upgrade=lambda _target: pytest.fail("verify must not run Alembic upgrade"),
        rollback=lambda: pytest.fail("verify must not need rollback"),
    )

    assert (result.result, result.exit_code, result.pending_revisions) == (
        "verified",
        EXIT_SUCCESS,
        (),
    )
    assert events == ["lock-enter", "lock-exit"]


@pytest.mark.parametrize(
    ("migration_request", "heads"),
    [
        (_request("expand", expected_start_revision="r0"), (("r1",), ("r2",))),
        (_request("verify", target_revision="r1"), (("r1",), ("r2",))),
        (_request("expand"), ((), ("r2",))),
        (_request("expand"), (("r1", "other"), ("r2",))),
        (_request("expand"), (("r1",), ("r2", "other"))),
    ],
)
def test_invalid_start_target_or_head_never_reaches_upgrade(
    migration_request: MigrationRunnerRequest,
    heads: tuple[tuple[str, ...], tuple[str, ...]],
) -> None:
    upgraded: list[str] = []

    result = execute_migration(
        migration_request,
        lock=_lock([]),
        read_heads=lambda: heads,
        load_pending=lambda: pytest.fail("invalid heads must not load pending range"),
        upgrade=upgraded.append,
        rollback=lambda: None,
    )

    assert (result.result, result.exit_code, upgraded) == ("rejected", EXIT_INVALID, [])


@pytest.mark.parametrize("mode", ["expand", "contract"])
def test_empty_apply_range_is_not_a_successful_migration(
    mode: Literal["expand", "contract", "verify"],
) -> None:
    result = execute_migration(
        _request(mode, expected_start_revision="r2"),
        lock=_lock([]),
        read_heads=lambda: (("r2",), ("r2",)),
        load_pending=lambda: _pending((), "none"),
        upgrade=lambda _target: pytest.fail("empty range must not be upgraded"),
        rollback=lambda: pytest.fail("empty range must not be rolled back"),
    )

    assert (result.result, result.exit_code) == ("no_changes", EXIT_NO_CHANGES)


@pytest.mark.parametrize(
    "failure",
    ["upgrade", "post_upgrade_head"],
)
def test_execution_or_completion_failure_is_never_reported_as_applied(
    failure: str,
) -> None:
    rollback_count: list[None] = []
    if failure == "upgrade":
        heads: Iterator[tuple[tuple[str, ...], tuple[str, ...]]] = iter(
            [(("r1",), ("r2",))]
        )

        def upgrade(_target: str) -> None:
            raise RuntimeError("fixture database failure")

    else:
        heads = iter([(("r1",), ("r2",)), (("r1",), ("r2",))])

        def upgrade(_target: str) -> None:
            return None

    result = execute_migration(
        _request("expand"),
        lock=_lock([]),
        read_heads=lambda: next(heads),
        load_pending=lambda: _pending(("r2",), "expand"),
        upgrade=upgrade,
        rollback=lambda: rollback_count.append(None),
    )

    assert (result.result, result.exit_code, rollback_count) == (
        "failed",
        EXIT_MIGRATION_FAILED,
        [None],
    )


def test_lock_conflict_is_distinct_and_never_runs_upgrade() -> None:
    class ConflictLock:
        def __enter__(self) -> None:
            raise MigrationLockUnavailable("test lock")

        def __exit__(self, *_args: object) -> None:
            return None

    result = execute_migration(
        _request("expand"),
        lock=ConflictLock(),
        read_heads=lambda: pytest.fail("lock conflict must stop before DB inspection"),
        load_pending=lambda: pytest.fail(
            "lock conflict must stop before classification"
        ),
        upgrade=lambda _target: pytest.fail("lock conflict must stop before upgrade"),
        rollback=lambda: pytest.fail("lock conflict must not roll back"),
    )

    assert (result.result, result.exit_code) == ("rejected", EXIT_LOCK_CONFLICT)


@pytest.mark.parametrize(
    "overrides",
    [
        {"protocol_version": 2},
        {"mode": "manual"},
        {"mode": "expand", "expected_start_revision": None},
        {"target_revision": ""},
        {"migration_tree_oid": "not-a-tree"},
    ],
)
def test_request_requires_explicit_valid_protocol_and_mode(
    overrides: dict[str, object],
) -> None:
    values: dict[str, object] = {
        "protocol_version": 1,
        "mode": "verify",
        "expected_start_revision": None,
        "target_revision": "r2",
        "migration_tree_oid": _TREE_OID,
    }
    values.update(overrides)

    with pytest.raises(ValidationError):
        MigrationRunnerRequest(**values)


def test_request_loader_uses_only_the_migration_runner_contract_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for variable in (
        "MIGRATION_PROTOCOL_VERSION",
        "MIGRATION_MODE",
        "MIGRATION_EXPECTED_START_REVISION",
        "MIGRATION_TARGET_REVISION",
        "MIGRATION_TREE_OID",
    ):
        monkeypatch.delenv(variable, raising=False)
    monkeypatch.setenv("MIGRATION_PROTOCOL_VERSION", "1")
    monkeypatch.setenv("MIGRATION_MODE", "verify")
    monkeypatch.setenv("MIGRATION_TARGET_REVISION", "r2")
    monkeypatch.setenv("MIGRATION_TREE_OID", _TREE_OID)

    request = load_migration_runner_request()

    assert request == _request("verify", expected_start_revision=None)


@pytest.mark.parametrize(
    "missing",
    ["MIGRATION_PROTOCOL_VERSION", "MIGRATION_MODE"],
)
def test_main_rejects_missing_required_request_before_database_setup(
    missing: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    values = {
        "MIGRATION_PROTOCOL_VERSION": "1",
        "MIGRATION_MODE": "verify",
        "MIGRATION_TARGET_REVISION": "r2",
        "MIGRATION_TREE_OID": _TREE_OID,
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv(missing, raising=False)

    def fail_database_setup(*_args: object, **_kwargs: object) -> None:
        pytest.fail("invalid request must not create a database engine")

    monkeypatch.setattr(runner, "create_migration_engine", fail_database_setup)

    assert main([]) == EXIT_INVALID
    assert json.loads(capsys.readouterr().out)["exit_code"] == EXIT_INVALID


def test_protocol_probe_and_invalid_cli_never_load_settings_or_echo_input(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def fail_loader() -> None:
        pytest.fail("CLI parsing must precede settings or DB setup")

    monkeypatch.setattr(runner, "load_migration_runner_request", fail_loader)

    assert main(["--protocol-version"]) == EXIT_SUCCESS
    protocol_output = json.loads(capsys.readouterr().out)
    assert protocol_output == {"protocol_version": PROTOCOL_VERSION}

    secret_like_argument = "postgresql://runner-secret@example.invalid/db"
    assert main([f"--unknown={secret_like_argument}"]) == EXIT_INVALID
    captured = capsys.readouterr()
    assert secret_like_argument not in captured.out + captured.err

    for name, value in {
        "MIGRATION_PROTOCOL_VERSION": "1",
        "MIGRATION_MODE": "verify",
        "MIGRATION_TARGET_REVISION": "r2",
        "MIGRATION_TREE_OID": _TREE_OID,
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(
        runner,
        "load_migration_runner_request",
        load_migration_runner_request,
    )

    async def fail_run(_request: MigrationRunnerRequest) -> object:
        raise RuntimeError("postgresql://runtime-secret@example.invalid/db")

    monkeypatch.setattr(runner, "run", fail_run)

    assert main([]) == EXIT_MIGRATION_FAILED
    captured = capsys.readouterr()
    assert "postgresql://runtime-secret@example.invalid/db" not in (
        captured.out + captured.err
    )
