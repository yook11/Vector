"""Alembic revision の expand/contract gate。

PR では変更された revision file を分類し、本番では DB の current revision から
head までの pending range 全体を分類する。自動適用できるのは明示的に
``MIGRATION_KIND = "expand"`` を宣言し、upgrade body に破壊系 operation が無い
revision だけ。
"""

from __future__ import annotations

import argparse
import ast
import asyncio
import json
import re
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from alembic.script.revision import RevisionError

MigrationKind = Literal["expand", "contract", "unknown"]
MigrationDecision = Literal["none", "expand", "manual", "invalid"]

_DESTRUCTIVE_SQL_RE = re.compile(
    r"\b(DROP|TRUNCATE|DELETE|UPDATE|ALTER)\b|\bINSERT\b[\s\S]*\bSELECT\b",
    re.IGNORECASE,
)
_ALLOWLISTED_SQL_RE = re.compile(r"^\s*(SET|COMMENT)\b", re.IGNORECASE)
_MULTI_STATEMENT_SQL_RE = re.compile(r";\s*\S")
_CLI_REVISION_PATH_RE = re.compile(r"^(?:backend/)?alembic/versions/[A-Za-z0-9_]+\.py$")
_DROP_OP_PREFIX = "drop_"
_BLOCKED_CONSTRAINT_OPS = frozenset(
    {
        "create_unique_constraint",
        "create_foreign_key",
        "create_check_constraint",
    }
)
_ALLOWED_OPS = frozenset({"create_table"})
_ALLOWED_TYPE_CONSTRUCTORS = frozenset({"HALFVEC", "JSONB", "PgUUID"})
_ALLOWED_SERVER_DEFAULT_CALLS = frozenset(
    {"sa.false", "sa.func.now", "sa.text", "sa.true"}
)
_ALLOWED_SA_CONSTRUCTORS = frozenset(
    {
        "sa.BigInteger",
        "sa.Boolean",
        "sa.CheckConstraint",
        "sa.Column",
        "sa.Date",
        "sa.DateTime",
        "sa.Enum",
        "sa.Float",
        "sa.ForeignKey",
        "sa.ForeignKeyConstraint",
        "sa.Identity",
        "sa.Index",
        "sa.Integer",
        "sa.JSON",
        "sa.LargeBinary",
        "sa.Numeric",
        "sa.PrimaryKeyConstraint",
        "sa.SmallInteger",
        "sa.String",
        "sa.Text",
        "sa.Time",
        "sa.UniqueConstraint",
        "sa.Uuid",
        "sa.false",
        "sa.func.now",
        "sa.text",
        "sa.true",
    }
)


@dataclass(frozen=True, slots=True)
class Classification:
    """1 revision file の分類結果。"""

    path: Path
    kind: MigrationKind
    declared_kind: str | None
    reasons: tuple[str, ...]
    mislabelled_expand: bool = False

    @property
    def auto_allowed(self) -> bool:
        """本番自動適用してよい expand revision か。"""
        return self.kind == "expand" and not self.reasons


@dataclass(frozen=True, slots=True)
class ChangedMigrationDecision:
    """changed/pending revision集合をrelease判断へ渡す機械可読結果。"""

    decision: MigrationDecision
    revisions: tuple[Classification, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "decision": self.decision,
            "revisions": [
                {
                    "path": str(result.path),
                    "kind": result.kind,
                    "declared_kind": result.declared_kind,
                    "reasons": list(result.reasons),
                }
                for result in self.revisions
            ],
        }


class MigrationGateError(RuntimeError):
    """pending range の列挙など、分類以前の gate 失敗。"""


def classify(path: str | Path) -> Classification:
    """migration revision file を分類する。"""
    revision_path = Path(path)
    try:
        source = revision_path.read_text(encoding="utf-8")
    except OSError as exc:
        return Classification(
            path=revision_path,
            kind="unknown",
            declared_kind=None,
            reasons=(f"file cannot be read: {exc}",),
        )

    try:
        tree = ast.parse(source, filename=str(revision_path))
    except SyntaxError as exc:
        return Classification(
            path=revision_path,
            kind="unknown",
            declared_kind=None,
            reasons=(f"file cannot be parsed: {exc.msg}",),
        )

    declared_kind, declaration_reason = _read_declared_kind(tree)
    upgrade = _find_function(tree, "upgrade")
    structural_reasons: list[str] = []
    if declaration_reason is not None:
        structural_reasons.append(declaration_reason)
    if upgrade is None:
        structural_reasons.append("upgrade() is missing")

    if declared_kind not in {"expand", "contract"}:
        return Classification(
            path=revision_path,
            kind="unknown",
            declared_kind=declared_kind,
            reasons=tuple(structural_reasons),
        )
    if upgrade is None:
        return Classification(
            path=revision_path,
            kind="unknown",
            declared_kind=declared_kind,
            reasons=tuple(structural_reasons),
        )

    backstop_reasons = _scan_upgrade_body(upgrade)
    if declared_kind == "contract":
        return Classification(
            path=revision_path,
            kind="contract",
            declared_kind=declared_kind,
            reasons=tuple(backstop_reasons),
        )
    if backstop_reasons:
        return Classification(
            path=revision_path,
            kind="contract",
            declared_kind=declared_kind,
            reasons=tuple(backstop_reasons),
            mislabelled_expand=True,
        )
    return Classification(
        path=revision_path,
        kind="expand",
        declared_kind=declared_kind,
        reasons=(),
    )


def decide_changed_migrations(
    paths: Sequence[str | Path],
) -> ChangedMigrationDecision:
    """revision集合をnone/expand/manual/invalidへ分類する。"""
    classifications = tuple(classify(path) for path in paths)
    if not classifications:
        decision: MigrationDecision = "none"
    elif any(
        result.kind == "unknown" or result.mislabelled_expand
        for result in classifications
    ):
        decision = "invalid"
    elif all(result.auto_allowed for result in classifications):
        decision = "expand"
    else:
        decision = "manual"
    return ChangedMigrationDecision(decision=decision, revisions=classifications)


def _read_declared_kind(tree: ast.Module) -> tuple[str | None, str | None]:
    """module-level MIGRATION_KIND 宣言を読む。"""
    for node in tree.body:
        if isinstance(node, ast.Assign):
            if not any(_is_name(target, "MIGRATION_KIND") for target in node.targets):
                continue
            return _literal_string(node.value), _declaration_reason(node.value)
        if isinstance(node, ast.AnnAssign) and _is_name(node.target, "MIGRATION_KIND"):
            return _literal_string(node.value), _declaration_reason(node.value)
    return None, "MIGRATION_KIND is missing"


def _declaration_reason(value: ast.AST | None) -> str | None:
    declared = _literal_string(value)
    if declared in {"expand", "contract"}:
        return None
    if declared is None:
        return "MIGRATION_KIND must be literal 'expand' or 'contract'"
    return "MIGRATION_KIND has an invalid literal value"


def _find_function(
    tree: ast.Module,
    name: str,
) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    """module-level function を探す。"""
    for node in tree.body:
        if (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == name
        ):
            return node
    return None


def _scan_upgrade_body(node: ast.FunctionDef | ast.AsyncFunctionDef) -> tuple[str, ...]:
    """upgrade() body 内の破壊系 operation を fail-closed に検出する。"""
    reasons: list[str] = []
    for child in ast.walk(node):
        if not isinstance(child, ast.Call):
            continue
        reasons.extend(_classify_call(child))
    return tuple(dict.fromkeys(reasons))


def _classify_call(call: ast.Call) -> list[str]:
    reasons: list[str] = []
    op_name = _op_call_name(call)
    if op_name is not None:
        reasons.extend(_classify_op_call(op_name, call))
        return reasons

    if isinstance(call.func, ast.Attribute) and call.func.attr == "execute":
        return ["data migration via bind/connection.execute is not auto-allowed"]
    if _is_allowlisted_schema_constructor(call.func):
        return []
    return ["non-allowlisted helper call is manual-only"]


def _classify_op_call(op_name: str, call: ast.Call) -> list[str]:
    if op_name.startswith(_DROP_OP_PREFIX):
        return [f"op.{op_name} is destructive"]
    if op_name == "rename_table":
        return ["op.rename_table is destructive"]
    if op_name == "alter_column":
        return _classify_alter_column(call)
    if op_name in _BLOCKED_CONSTRAINT_OPS:
        return [f"op.{op_name} can validate existing rows and is manual-only"]
    if op_name == "create_index":
        return _classify_create_index(call)
    if op_name == "add_column":
        return _classify_add_column(call)
    if op_name == "get_bind":
        return ["op.get_bind indicates a data migration and is manual-only"]
    if op_name == "execute":
        return _classify_op_execute(call)
    if op_name in _ALLOWED_OPS:
        return []
    return ["non-allowlisted Alembic operation is manual-only"]


def _is_allowlisted_schema_constructor(node: ast.AST) -> bool:
    if isinstance(node, ast.Name):
        return node.id in _ALLOWED_TYPE_CONSTRUCTORS
    qualified_name = _qualified_name(node)
    return qualified_name in _ALLOWED_SA_CONSTRUCTORS


def _qualified_name(node: ast.AST) -> str | None:
    parts: list[str] = []
    current = node
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if not isinstance(current, ast.Name):
        return None
    parts.append(current.id)
    return ".".join(reversed(parts))


def _classify_alter_column(call: ast.Call) -> list[str]:
    if len(call.args) != 2 or not all(
        _literal_string(argument) is not None for argument in call.args
    ):
        return ["op.alter_column target must be two literal names"]

    allowed_keywords = {
        "existing_nullable",
        "existing_server_default",
        "existing_type",
        "nullable",
        "schema",
    }
    unsupported = sorted(
        keyword.arg or "**kwargs"
        for keyword in call.keywords
        if keyword.arg not in allowed_keywords
    )
    if unsupported:
        return [
            "op.alter_column unsupported change keywords are manual-only: "
            + ", ".join(unsupported)
        ]

    nullable_kw = _keyword(call, "nullable")
    if nullable_kw is None:
        return ["op.alter_column only nullable=True is auto-allowed"]
    nullable_value = _literal_bool(nullable_kw.value)
    if nullable_value is False:
        return ["op.alter_column(nullable=False) sets NOT NULL"]
    if nullable_value is None:
        return ["op.alter_column(nullable=...) is dynamic and manual-only"]
    return []


def _classify_create_index(call: ast.Call) -> list[str]:
    if len(call.args) != 3 or not all(
        _literal_string(argument) is not None for argument in call.args[:2]
    ):
        return ["op.create_index requires literal index and table names"]
    columns = call.args[2]
    if not isinstance(columns, (ast.List, ast.Tuple)) or not columns.elts:
        return ["op.create_index columns must be a non-empty literal list"]
    if any(_literal_string(column) is None for column in columns.elts):
        return ["op.create_index expression columns are manual-only"]

    allowed_keywords = {"postgresql_concurrently", "schema", "unique"}
    unsupported = sorted(
        keyword.arg or "**kwargs"
        for keyword in call.keywords
        if keyword.arg not in allowed_keywords
    )
    if unsupported:
        return [
            "op.create_index unsupported options are manual-only: "
            + ", ".join(unsupported)
        ]

    unique_kw = _keyword(call, "unique")
    if unique_kw is not None and _literal_bool(unique_kw.value) is not False:
        return ["op.create_index unique=True or dynamic is manual-only"]
    schema_kw = _keyword(call, "schema")
    if schema_kw is not None and _literal_string(schema_kw.value) is None:
        return ["op.create_index schema must be a literal name"]
    concurrently_kw = _keyword(call, "postgresql_concurrently")
    if concurrently_kw is not None and _literal_bool(concurrently_kw.value) is True:
        return []
    return ["op.create_index without postgresql_concurrently=True is manual-only"]


def _classify_add_column(call: ast.Call) -> list[str]:
    if len(call.args) != 2 or _literal_string(call.args[0]) is None:
        return ["op.add_column requires a literal table and one column"]
    if call.keywords:
        return ["op.add_column operation keywords are manual-only"]
    column = call.args[1]
    if not isinstance(column, ast.Call) or _qualified_name(column.func) != "sa.Column":
        return ["op.add_column column argument is dynamic and manual-only"]
    if len(column.args) != 2 or _literal_string(column.args[0]) is None:
        return [
            "op.add_column constraints or dynamic column definitions are manual-only"
        ]

    allowed_column_keywords = {"nullable", "server_default"}
    unsupported = sorted(
        keyword.arg or "**kwargs"
        for keyword in column.keywords
        if keyword.arg not in allowed_column_keywords
    )
    if unsupported:
        return [
            "op.add_column constraint-bearing or unsupported keywords are manual-only: "
            + ", ".join(unsupported)
        ]

    nullable_kw = _keyword(column, "nullable")
    nullable = True if nullable_kw is None else _literal_bool(nullable_kw.value)
    if nullable is None:
        return ["op.add_column(nullable=...) is dynamic and manual-only"]
    server_default_kw = _keyword(column, "server_default")
    if server_default_kw is not None and not _is_static_server_default(
        server_default_kw.value
    ):
        return ["op.add_column server_default must be a non-null static value"]
    if nullable is False and server_default_kw is None:
        return ["op.add_column(nullable=False) without server_default is manual-only"]
    return []


def _is_static_server_default(node: ast.AST) -> bool:
    if isinstance(node, ast.Constant):
        return node.value is not None and isinstance(
            node.value, (str, int, float, bool)
        )
    if not isinstance(node, ast.Call):
        return False
    qualified_name = _qualified_name(node.func)
    if qualified_name not in _ALLOWED_SERVER_DEFAULT_CALLS or node.keywords:
        return False
    if qualified_name in {"sa.false", "sa.func.now", "sa.true"}:
        return not node.args
    return len(node.args) == 1 and _literal_string(node.args[0]) is not None


def _classify_op_execute(call: ast.Call) -> list[str]:
    if not call.args:
        return ["op.execute without SQL text is manual-only"]
    sql = _literal_sql(call.args[0])
    if sql is None:
        return ["op.execute SQL is dynamic and manual-only"]
    if _is_allowlisted_sql(sql):
        return []
    if _DESTRUCTIVE_SQL_RE.search(sql):
        return ["op.execute contains destructive or data-changing SQL"]
    return ["op.execute raw SQL is not allowlisted for auto migration"]


def _is_allowlisted_sql(sql: str) -> bool:
    return bool(_ALLOWLISTED_SQL_RE.search(sql)) and not _MULTI_STATEMENT_SQL_RE.search(
        sql
    )


def _literal_sql(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Call) and node.args:
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr == "text":
            return _literal_sql(node.args[0])
        if isinstance(func, ast.Name) and func.id == "text":
            return _literal_sql(node.args[0])
    return None


def _op_call_name(call: ast.Call) -> str | None:
    func = call.func
    if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
        if func.value.id == "op":
            return func.attr
    return None


def _keyword(call: ast.Call, name: str) -> ast.keyword | None:
    for keyword in call.keywords:
        if keyword.arg == name:
            return keyword
    return None


def _literal_string(node: ast.AST | None) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _literal_bool(node: ast.AST) -> bool | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, bool):
        return node.value
    return None


def _is_name(node: ast.AST, name: str) -> bool:
    return isinstance(node, ast.Name) and node.id == name


def _resolve_cli_path(raw_path: str) -> Path:
    """repo root / backend working-directory の両方から path を解決する。"""
    if _CLI_REVISION_PATH_RE.fullmatch(raw_path) is None:
        raise ValueError("migration path is outside the allowed revision path format")
    path = Path(raw_path)
    if not path.exists() and raw_path.startswith("backend/"):
        backend_relative = Path(raw_path.removeprefix("backend/"))
        if backend_relative.exists():
            path = backend_relative
    if not path.is_file() or path.is_symlink():
        raise ValueError("migration path must name a regular revision file")
    resolved = path.resolve()
    allowed_directories = {
        (Path.cwd() / "alembic" / "versions").resolve(),
        (Path.cwd() / "backend" / "alembic" / "versions").resolve(),
    }
    if resolved.parent not in allowed_directories:
        raise ValueError("migration path resolves outside the revision directory")
    return path


def _load_files_json(value: str) -> list[str]:
    if value.strip() == "":
        return []
    loaded = json.loads(value)
    if not isinstance(loaded, list) or not all(
        isinstance(item, str) for item in loaded
    ):
        raise ValueError("--files-json must be a JSON string array")
    return loaded


def _print_classifications(classifications: Sequence[Classification]) -> None:
    if not classifications:
        print("No migration revision files to classify.")
        return
    for result in classifications:
        auto = "yes" if result.auto_allowed else "no"
        if result.declared_kind in {"expand", "contract"}:
            declared = result.declared_kind
        elif result.declared_kind is None:
            declared = "-"
        else:
            declared = "<invalid>"
        print(
            f"{result.path}: kind={result.kind} declared={declared} auto_allowed={auto}"
        )
        for reason in result.reasons:
            print(f"  - {reason}")


def _files_gate(paths: Sequence[str], *, decision_output: str | None = None) -> int:
    resolved = [_resolve_cli_path(path) for path in paths]
    decision = decide_changed_migrations(resolved)
    _print_classifications(decision.revisions)
    if decision_output is not None:
        Path(decision_output).write_text(
            json.dumps(decision.as_dict(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    if decision.decision == "invalid":
        print(
            "Migration file gate failed: undeclared/unknown or mislabelled "
            "expand revision."
        )
        return 1
    return 0


async def _pending_gate() -> int:
    paths = await _pending_revision_paths()
    classifications = [classify(path) for path in paths]
    _print_classifications(classifications)
    blocked = [result for result in classifications if not result.auto_allowed]
    if blocked:
        print(
            "Pending migration gate failed: non-expand pending revision "
            "requires manual apply."
        )
        return 1
    print("Pending migration gate passed: pending range is empty or expand-only.")
    return 0


async def _pending_revision_paths() -> list[Path]:
    script = _script_directory()
    current_heads = await _current_db_heads()
    return pending_revision_paths(script, current_heads)


def pending_revision_paths(
    script: ScriptDirectory,
    current_heads: Sequence[str],
) -> list[Path]:
    """DB currentからsingle script headまでのrevision fileを順番に返す。"""
    script_heads = script.get_heads()
    if len(script_heads) != 1:
        raise MigrationGateError(f"expected single Alembic head, got {script_heads!r}")
    if len(current_heads) > 1:
        raise MigrationGateError(
            f"expected single DB current head, got {current_heads!r}"
        )

    lower: str = current_heads[0] if current_heads else "base"
    upper = script_heads[0]
    if lower == upper:
        print(f"DB current={lower}; script head={upper}; pending=0")
        return []

    try:
        revisions = list(script.iterate_revisions(upper, lower))
    except RevisionError as exc:
        raise MigrationGateError(
            "DB current revision is not resolvable from the script head"
        ) from exc
    paths: list[Path] = []
    for revision in reversed(revisions):
        if revision.path is None:
            raise MigrationGateError(f"revision {revision.revision} has no file path")
        paths.append(Path(revision.path))
    print(
        f"DB current={lower}; script head={upper}; pending={len(paths)} "
        f"({', '.join(rev.revision for rev in reversed(revisions))})"
    )
    return paths


def _script_directory() -> ScriptDirectory:
    config_path = Path("alembic.ini")
    if not config_path.exists():
        raise MigrationGateError("alembic.ini not found; run from backend/")
    return ScriptDirectory.from_config(Config(str(config_path)))


async def _current_db_heads() -> tuple[str, ...]:
    from app.migration_config import load_migration_settings
    from app.migration_db import create_migration_engine

    engine = create_migration_engine(load_migration_settings())
    try:
        async with engine.connect() as connection:
            return await connection.run_sync(_current_heads_from_sync_connection)
    finally:
        await engine.dispose()


def _current_heads_from_sync_connection(connection: object) -> tuple[str, ...]:
    context = MigrationContext.configure(connection)
    return tuple(context.get_current_heads())


def _parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--files", nargs="*", help="Migration revision files to classify")
    mode.add_argument("--files-json", help="JSON array of migration revision files")
    mode.add_argument(
        "--pending",
        action="store_true",
        help="Classify DB pending range",
    )
    parser.add_argument(
        "--decision-output",
        help="Write the machine-readable migration decision JSON to this path.",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    try:
        if args.files is not None:
            return _files_gate(args.files, decision_output=args.decision_output)
        if args.files_json is not None:
            return _files_gate(
                _load_files_json(args.files_json),
                decision_output=args.decision_output,
            )
        if args.decision_output is not None:
            raise ValueError(
                "--decision-output is only valid with --files/--files-json"
            )
        return asyncio.run(_pending_gate())
    except (MigrationGateError, ValueError, json.JSONDecodeError) as exc:
        print(f"Migration gate failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
