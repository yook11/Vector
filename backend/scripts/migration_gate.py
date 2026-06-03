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

MigrationKind = Literal["expand", "contract", "unknown"]

_DESTRUCTIVE_SQL_RE = re.compile(
    r"\b(DROP|TRUNCATE|DELETE|UPDATE|ALTER)\b|\bINSERT\b[\s\S]*\bSELECT\b",
    re.IGNORECASE,
)
_ALLOWLISTED_SQL_RE = re.compile(r"^\s*(SET|COMMENT)\b", re.IGNORECASE)
_DROP_OP_PREFIX = "drop_"
_BLOCKED_CONSTRAINT_OPS = frozenset(
    {
        "create_unique_constraint",
        "create_foreign_key",
        "create_check_constraint",
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
    return f"MIGRATION_KIND has invalid value: {declared!r}"


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
        reasons.append("data migration via bind/connection.execute is not auto-allowed")
    return reasons


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
    return []


def _classify_alter_column(call: ast.Call) -> list[str]:
    reasons: list[str] = []
    for blocked_kw in ("type_", "new_column_name"):
        if _has_keyword(call, blocked_kw):
            reasons.append(f"op.alter_column({blocked_kw}=...) is contract-only")
    nullable_kw = _keyword(call, "nullable")
    if nullable_kw is not None:
        nullable_value = _literal_bool(nullable_kw.value)
        if nullable_value is False:
            reasons.append("op.alter_column(nullable=False) sets NOT NULL")
        elif nullable_value is None:
            reasons.append("op.alter_column(nullable=...) is dynamic and manual-only")
    return reasons


def _classify_create_index(call: ast.Call) -> list[str]:
    concurrently_kw = _keyword(call, "postgresql_concurrently")
    if concurrently_kw is not None and _literal_bool(concurrently_kw.value) is True:
        return []
    return ["op.create_index without postgresql_concurrently=True is manual-only"]


def _classify_add_column(call: ast.Call) -> list[str]:
    if len(call.args) < 2:
        return ["op.add_column column argument is missing"]
    column = call.args[1]
    if not isinstance(column, ast.Call):
        return ["op.add_column column argument is dynamic and manual-only"]

    nullable_kw = _keyword(column, "nullable")
    nullable = True if nullable_kw is None else _literal_bool(nullable_kw.value)
    if nullable is None:
        return ["op.add_column(nullable=...) is dynamic and manual-only"]
    if nullable is False and not _has_keyword(column, "server_default"):
        return ["op.add_column(nullable=False) without server_default is manual-only"]
    return []


def _classify_op_execute(call: ast.Call) -> list[str]:
    if not call.args:
        return ["op.execute without SQL text is manual-only"]
    sql = _literal_sql(call.args[0])
    if sql is None:
        return ["op.execute SQL is dynamic and manual-only"]
    if _ALLOWLISTED_SQL_RE.search(sql):
        return []
    if _DESTRUCTIVE_SQL_RE.search(sql):
        return ["op.execute contains destructive or data-changing SQL"]
    return ["op.execute raw SQL is not allowlisted for auto migration"]


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


def _has_keyword(call: ast.Call, name: str) -> bool:
    return _keyword(call, name) is not None


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
    path = Path(raw_path)
    if path.exists():
        return path
    if raw_path.startswith("backend/"):
        backend_relative = Path(raw_path.removeprefix("backend/"))
        if backend_relative.exists():
            return backend_relative
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
        declared = result.declared_kind if result.declared_kind is not None else "-"
        print(
            f"{result.path}: kind={result.kind} declared={declared} auto_allowed={auto}"
        )
        for reason in result.reasons:
            print(f"  - {reason}")


def _files_gate(paths: Sequence[str]) -> int:
    classifications = [classify(_resolve_cli_path(path)) for path in paths]
    _print_classifications(classifications)
    failed = [
        result
        for result in classifications
        if result.kind == "unknown" or result.mislabelled_expand
    ]
    if failed:
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
    script_heads = script.get_heads()
    if len(script_heads) != 1:
        raise MigrationGateError(f"expected single Alembic head, got {script_heads!r}")
    current_heads = await _current_db_heads()
    if len(current_heads) > 1:
        raise MigrationGateError(
            f"expected single DB current head, got {current_heads!r}"
        )

    lower: str = current_heads[0] if current_heads else "base"
    upper = script_heads[0]
    if lower == upper:
        print(f"DB current={lower}; script head={upper}; pending=0")
        return []

    revisions = list(script.iterate_revisions(upper, lower))
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
    from app.config import settings
    from app.db_ssl import create_app_engine

    url = settings.migration_database_url or settings.database_url
    engine = create_app_engine(url, application_name="vector-cli-migration-gate")
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
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    try:
        if args.files is not None:
            return _files_gate(args.files)
        if args.files_json is not None:
            return _files_gate(_load_files_json(args.files_json))
        return asyncio.run(_pending_gate())
    except (MigrationGateError, ValueError, json.JSONDecodeError) as exc:
        print(f"Migration gate failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
