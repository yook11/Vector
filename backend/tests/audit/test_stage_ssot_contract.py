"""Audit stage SSoT refactor の focused static contract tests。"""

from __future__ import annotations

import ast
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[3]
_APP = _ROOT / "backend" / "app"
_AUDIT_STAGE_DIR = _APP / "audit" / "stages"

_AUDIT_STAGE_REPOSITORY_FILES = [
    _AUDIT_STAGE_DIR / name
    for name in (
        "briefing.py",
        "trend_discovery.py",
    )
]

_EXPECTED_REPOSITORY_STAGE_CONSTANTS = {
    "briefing.py": {
        "BriefingAuditRepository": {"STAGE": "BRIEFING"},
    },
    "trend_discovery.py": {
        "TrendDiscoveryAuditRepository": {"STAGE": "TREND_DISCOVERY"},
    },
}


def _parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(), filename=str(path))


def _annotate_parents(tree: ast.AST) -> None:
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            child.parent = parent  # type: ignore[attr-defined]


def _enclosing_function(node: ast.AST) -> ast.AsyncFunctionDef | ast.FunctionDef | None:
    current = getattr(node, "parent", None)
    while current is not None:
        if isinstance(current, ast.AsyncFunctionDef | ast.FunctionDef):
            return current
        current = getattr(current, "parent", None)
    return None


def _is_self_events_append(call: ast.Call) -> bool:
    return (
        isinstance(call.func, ast.Attribute)
        and call.func.attr == "append"
        and isinstance(call.func.value, ast.Attribute)
        and call.func.value.attr == "_events"
        and isinstance(call.func.value.value, ast.Name)
        and call.func.value.value.id == "self"
    )


def _stage_keyword(call: ast.Call) -> ast.keyword | None:
    return next((kw for kw in call.keywords if kw.arg == "stage"), None)


def _stage_member_name(value: ast.AST) -> str | None:
    if (
        isinstance(value, ast.Attribute)
        and isinstance(value.value, ast.Name)
        and value.value.id == "Stage"
    ):
        return value.attr
    return None


def _self_stage_constant_name(value: ast.AST) -> str | None:
    if (
        isinstance(value, ast.Attribute)
        and isinstance(value.value, ast.Name)
        and value.value.id == "self"
        and value.attr in {"STAGE", "BACKFILL_STAGE"}
    ):
        return value.attr
    return None


def _class_stage_constants(tree: ast.Module, class_name: str) -> dict[str, str]:
    for node in tree.body:
        if not isinstance(node, ast.ClassDef) or node.name != class_name:
            continue
        constants: dict[str, str] = {}
        for stmt in node.body:
            if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                member = (
                    _stage_member_name(stmt.value) if stmt.value is not None else None
                )
                if member is not None:
                    constants[stmt.target.id] = member
            elif isinstance(stmt, ast.Assign):
                member = _stage_member_name(stmt.value)
                if member is None:
                    continue
                for target in stmt.targets:
                    if isinstance(target, ast.Name):
                        constants[target.id] = member
        return constants
    return {}


def test_audit_repositories_publish_stage_constants_as_event_stage_ssot() -> None:
    missing_or_wrong: list[str] = []
    for file_name, classes in _EXPECTED_REPOSITORY_STAGE_CONSTANTS.items():
        tree = _parse(_AUDIT_STAGE_DIR / file_name)
        for class_name, expected in classes.items():
            actual = _class_stage_constants(tree, class_name)
            for constant_name, stage_member in expected.items():
                if actual.get(constant_name) != stage_member:
                    missing_or_wrong.append(
                        f"{file_name}:{class_name}.{constant_name} "
                        f"expected Stage.{stage_member}, "
                        f"got {actual.get(constant_name)!r}"
                    )

    assert not missing_or_wrong


def test_events_append_stage_is_only_in_repository_private_append_funnels() -> None:
    violations: list[str] = []
    for path in _AUDIT_STAGE_REPOSITORY_FILES:
        tree = _parse(path)
        _annotate_parents(tree)
        for call in (node for node in ast.walk(tree) if isinstance(node, ast.Call)):
            if not _is_self_events_append(call) or _stage_keyword(call) is None:
                continue
            function = _enclosing_function(call)
            if function is None or not function.name.startswith("_append"):
                name = function.name if function is not None else "<module>"
                violations.append(f"{path.relative_to(_ROOT)}:{call.lineno} in {name}")

    assert not violations


def test_private_append_funnels_do_not_accept_stage_or_kwargs() -> None:
    violations: list[str] = []
    for path in _AUDIT_STAGE_REPOSITORY_FILES:
        for node in ast.walk(_parse(path)):
            if not isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef):
                continue
            if not node.name.startswith("_append"):
                continue
            if any(arg.arg == "stage" for arg in node.args.args + node.args.kwonlyargs):
                violations.append(
                    f"{path.relative_to(_ROOT)}:{node.lineno} stage param"
                )
            if node.args.kwarg is not None:
                violations.append(f"{path.relative_to(_ROOT)}:{node.lineno} **kwargs")

    assert not violations


def test_private_append_funnel_injects_only_repository_stage_constants() -> None:
    violations: list[str] = []
    for path in _AUDIT_STAGE_REPOSITORY_FILES:
        tree = _parse(path)
        _annotate_parents(tree)
        for call in (node for node in ast.walk(tree) if isinstance(node, ast.Call)):
            stage = _stage_keyword(call)
            function = _enclosing_function(call)
            if (
                not _is_self_events_append(call)
                or stage is None
                or function is None
                or not function.name.startswith("_append")
            ):
                continue
            if _self_stage_constant_name(stage.value) is None:
                violations.append(
                    f"{path.relative_to(_ROOT)}:{call.lineno} in {function.name}"
                )

    assert not violations


def test_failure_append_path_does_not_read_projection_stage() -> None:
    violations: list[str] = []
    for path in _AUDIT_STAGE_REPOSITORY_FILES:
        for node in ast.walk(_parse(path)):
            if (
                isinstance(node, ast.Attribute)
                and node.attr == "stage"
                and isinstance(node.value, ast.Name)
                and node.value.id == "projection"
            ):
                violations.append(f"{path.relative_to(_ROOT)}:{node.lineno}")

    assert not violations
