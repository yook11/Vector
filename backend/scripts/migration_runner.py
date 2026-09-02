"""明示protocolとmodeに拘束された適用を行い、承認・ledger・image照合は外側に委ねる。"""

from __future__ import annotations

import argparse
import asyncio
import io
import json
import logging
import sys
from collections.abc import Callable, Iterator, Sequence
from contextlib import (
    AbstractContextManager,
    contextmanager,
    redirect_stderr,
    redirect_stdout,
)
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Literal

from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from alembic.script.revision import RevisionError
from alembic.util.exc import CommandError
from pydantic import ValidationError
from sqlalchemy.engine import Connection

from alembic import command
from app.migration_config import (
    MIGRATION_RUNNER_PROTOCOL_VERSION,
    MigrationRunnerRequest,
    load_migration_runner_request,
    load_migration_settings,
    require_revision_id,
)
from app.migration_db import create_migration_engine
from app.migration_lock import MigrationLockUnavailable, migration_advisory_lock
from scripts.migration_gate import (
    ChangedMigrationDecision,
    MigrationGateError,
    decide_changed_migrations,
)

PROTOCOL_VERSION = MIGRATION_RUNNER_PROTOCOL_VERSION
EXIT_SUCCESS = 0
EXIT_INVALID = 2
EXIT_NO_CHANGES = 3
EXIT_LOCK_CONFLICT = 11
EXIT_MIGRATION_FAILED = 12


class MigrationRunnerContractError(ValueError):
    """CLIへ生の入力を持ち出さない実行契約違反。"""


@dataclass(frozen=True)
class PendingMigrationRange:
    revision_ids: tuple[str, ...]
    decision: ChangedMigrationDecision


@dataclass(frozen=True)
class MigrationRunResult:
    protocol_version: int = PROTOCOL_VERSION
    mode: str | None = None
    result: Literal["applied", "verified", "no_changes", "rejected", "failed"] = (
        "rejected"
    )
    start_revision: str | None = None
    end_revision: str | None = None
    target_revision: str | None = None
    pending_revisions: tuple[str, ...] = ()
    migration_tree_oid: str | None = None
    reason: str = "invalid_request"
    exit_code: int = EXIT_INVALID

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def _initial_result(request: MigrationRunnerRequest) -> MigrationRunResult:
    return MigrationRunResult(
        mode=request.mode,
        target_revision=request.target_revision,
        migration_tree_oid=request.migration_tree_oid,
    )


def _single_revision(heads: Sequence[str]) -> str:
    if len(heads) != 1:
        raise MigrationGateError("single_revision_required")
    try:
        return require_revision_id(heads[0])
    except (TypeError, ValueError):
        raise MigrationGateError("invalid_revision_id") from None


def execute_migration(
    request: MigrationRunnerRequest,
    *,
    lock: AbstractContextManager[None],
    read_heads: Callable[[], tuple[Sequence[str], Sequence[str]]],
    load_pending: Callable[[], PendingMigrationRange],
    upgrade: Callable[[str], None],
    rollback: Callable[[], None],
) -> MigrationRunResult:
    result = _initial_result(request)
    applying = False
    try:
        with lock:
            try:
                db_heads, script_heads = read_heads()
                start = _single_revision(db_heads)
                target = _single_revision(script_heads)
                result = replace(result, start_revision=start)
                if target != request.target_revision:
                    return replace(result, reason="target_revision_mismatch")
                if request.expected_start_revision not in (None, start):
                    return replace(result, reason="start_revision_mismatch")

                pending = load_pending()
                for revision in pending.revision_ids:
                    _single_revision((revision,))
                result = replace(result, pending_revisions=pending.revision_ids)
                decision = pending.decision.decision
                if decision == "invalid":
                    return replace(result, reason="invalid_pending_range")
                if request.mode == "verify":
                    if start != target or pending.revision_ids or decision != "none":
                        return replace(result, reason="verify_not_at_target")
                    return replace(
                        result,
                        result="verified",
                        end_revision=start,
                        reason="verified",
                        exit_code=EXIT_SUCCESS,
                    )
                if not pending.revision_ids:
                    if decision != "none" or start != target:
                        return replace(result, reason="invalid_pending_range")
                    return replace(
                        result,
                        result="no_changes",
                        end_revision=start,
                        reason="no_changes",
                        exit_code=EXIT_NO_CHANGES,
                    )
                expected = "expand" if request.mode == "expand" else "manual"
                if decision != expected:
                    return replace(result, reason="mode_range_mismatch")
                applying = True
                upgrade(target)
                db_heads, script_heads = read_heads()
                try:
                    end = _single_revision(db_heads)
                    final_head = _single_revision(script_heads)
                except MigrationGateError:
                    rollback()
                    return replace(
                        result,
                        result="failed",
                        reason="post_upgrade_revision_mismatch",
                        exit_code=EXIT_MIGRATION_FAILED,
                    )
                result = replace(result, end_revision=end)
                if end != target or final_head != target:
                    rollback()
                    return replace(
                        result,
                        result="failed",
                        reason="post_upgrade_revision_mismatch",
                        exit_code=EXIT_MIGRATION_FAILED,
                    )
                result = replace(
                    result, result="applied", reason="applied", exit_code=EXIT_SUCCESS
                )
            except Exception:
                # aborted transactionを閉じ、同一sessionでのlock解放を可能にする。
                rollback()
                raise
        return result
    except MigrationLockUnavailable:
        return replace(
            result,
            result="rejected",
            reason="lock_conflict",
            exit_code=EXIT_LOCK_CONFLICT,
        )
    except MigrationGateError:
        return replace(
            result,
            result="failed" if applying else "rejected",
            reason="execution_failed" if applying else "invalid_revision_range",
            exit_code=EXIT_MIGRATION_FAILED if applying else EXIT_INVALID,
        )
    except Exception:
        return replace(
            result,
            result="failed",
            reason="execution_failed",
            exit_code=EXIT_MIGRATION_FAILED,
        )


def _run_on_connection(
    connection: Connection,
    request: MigrationRunnerRequest,
    *,
    config: Config | None = None,
) -> MigrationRunResult:
    alembic_config = config if config is not None else Config("alembic.ini")
    try:
        script = ScriptDirectory.from_config(alembic_config)
    except (CommandError, OSError):
        return replace(_initial_result(request), reason="invalid_revision_range")

    def read_heads() -> tuple[tuple[str, ...], tuple[str, ...]]:
        current = tuple(MigrationContext.configure(connection).get_current_heads())
        try:
            heads = tuple(script.get_heads())
        except (CommandError, RevisionError):
            raise MigrationGateError("invalid_script_heads") from None
        return current, heads

    def load_pending() -> PendingMigrationRange:
        current, _ = read_heads()
        try:
            revisions = tuple(
                reversed(
                    tuple(script.iterate_revisions(request.target_revision, current[0]))
                )
            )
        except (CommandError, RevisionError):
            raise MigrationGateError("invalid_revision_range") from None
        if any(revision.path is None for revision in revisions):
            raise MigrationGateError("missing_revision_file")
        return PendingMigrationRange(
            revision_ids=tuple(revision.revision for revision in revisions),
            decision=decide_changed_migrations(
                [Path(revision.path) for revision in revisions]
            ),
        )

    def upgrade(target: str) -> None:
        # introspectionのtransactionを閉じてもsession advisory lockは保持される。
        connection.commit()
        alembic_config.attributes["connection"] = connection
        command.upgrade(alembic_config, target)

    return execute_migration(
        request,
        lock=migration_advisory_lock(connection),
        read_heads=read_heads,
        load_pending=load_pending,
        upgrade=upgrade,
        rollback=connection.rollback,
    )


async def run(request: MigrationRunnerRequest) -> MigrationRunResult:
    settings = load_migration_settings()
    if settings.env != "production":
        raise MigrationRunnerContractError("production_required")
    engine = create_migration_engine(settings)
    try:
        async with engine.connect() as connection:
            return await connection.run_sync(_run_on_connection, request)
    finally:
        await engine.dispose()


class _ArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise MigrationRunnerContractError("invalid_arguments")


class _DiscardedOutput(io.TextIOBase):
    def write(self, text: str) -> int:
        return len(text)


@contextmanager
def _private_runtime_output() -> Iterator[None]:
    """migrationの任意出力を公開せず、CLIは構造化結果と固定診断だけを出す。"""
    previous = logging.root.manager.disable
    logging.disable(sys.maxsize)
    try:
        sink = _DiscardedOutput()
        with redirect_stdout(sink), redirect_stderr(sink):
            yield
    finally:
        logging.disable(previous)


def main(argv: Sequence[str] | None = None) -> int:
    parser = _ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--protocol-version", action="store_true")
    result = MigrationRunResult()
    try:
        args = parser.parse_args(argv)
        if args.protocol_version:
            print(
                json.dumps(
                    {"protocol_version": PROTOCOL_VERSION}, separators=(",", ":")
                )
            )
            return EXIT_SUCCESS
        request = load_migration_runner_request()
        result = _initial_result(request)
        with _private_runtime_output():
            result = asyncio.run(run(request))
    except (MigrationRunnerContractError, ValidationError):
        result = replace(result, reason="invalid_request")
    except Exception:
        result = replace(
            result,
            result="failed",
            reason="execution_failed",
            exit_code=EXIT_MIGRATION_FAILED,
        )
    print(json.dumps(result.as_dict(), sort_keys=True))
    if result.exit_code != EXIT_SUCCESS:
        print(f"Migration runner stopped: {result.reason}", file=sys.stderr)
    return result.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
