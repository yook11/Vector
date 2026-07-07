"""``record_audit_dropped`` 配線の網羅性 oracle (AST per-handler 併設)。

PR3 の価値は「全ての ``*_audit_dropped`` ログ site に counter emit が併設される」こと。
``app/`` を AST で走査し、``except`` ハンドラ単位で drop-log 呼び出しと
``record_audit_dropped(...)`` が同じハンドラ内に併存することを固定する。

ハンドラ単位で閉じるため、同一ファイル内の相殺 (ある block の record を消し別 block に
足す) も、except 外 / 成功パスへの誤配置も検知する。event 名は文字列リテラルで判定する
ため大文字・数字を含む名前も拾う (動的生成名は静的に解決できないため対象外)。

stage の値そのものの正しさは本テストでは保証しない (代表 site の e2e + レビューで担保。
構造的保証は将来 fixed-stage repository が自身の STAGE を公開する方向で別 PR)。
capfire は使わない (純粋な静的解析)。
"""

from __future__ import annotations

import ast
from pathlib import Path

# drop site を増減したら更新する意図的 tripwire (現 SSoT 件数)。
EXPECTED_AUDIT_DROPPED_SITES = 24

_APP_DIR = Path(__file__).resolve().parents[2] / "app"


def _is_drop_log_call(node: ast.AST) -> bool:
    """``logger.<...>("..._audit_dropped", ...)`` の drop-log 呼び出しか。"""
    if not isinstance(node, ast.Call) or not node.args:
        return False
    first = node.args[0]
    return (
        isinstance(first, ast.Constant)
        and isinstance(first.value, str)
        and first.value.endswith("_audit_dropped")
    )


def _is_record_call(node: ast.AST) -> bool:
    """``record_audit_dropped(...)`` 呼び出しか。"""
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "record_audit_dropped"
    )


def _scan_handlers() -> list[tuple[str, int, bool, bool]]:
    """drop-log / record を含む except を (file, line, has_log, has_record) で返す。"""
    handlers: list[tuple[str, int, bool, bool]] = []
    for path in _APP_DIR.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        rel = path.relative_to(_APP_DIR).as_posix()
        for node in ast.walk(tree):
            if not isinstance(node, ast.ExceptHandler):
                continue
            calls = [c for c in ast.walk(node) if isinstance(c, ast.Call)]
            has_log = any(_is_drop_log_call(c) for c in calls)
            has_record = any(_is_record_call(c) for c in calls)
            if has_log or has_record:
                handlers.append((rel, node.lineno, has_log, has_record))
    return handlers


# 決定的な静的走査のため module load 時に 1 度だけ実行する。
_HANDLERS = _scan_handlers()


def test_drop_log_and_counter_are_colocated_per_handler() -> None:
    """drop-log を持つ except は同じハンドラ内に counter 呼び出しを持つ (相互)。"""
    mismatches = [h for h in _HANDLERS if h[2] != h[3]]
    assert not mismatches, f"併設不一致 handler (file, line, log, rec): {mismatches}"


def test_total_audit_dropped_sites_match_ssot() -> None:
    """drop-log を持つ except handler 数が現 SSoT 件数に一致する (増減 tripwire)。"""
    drop_sites = sum(1 for h in _HANDLERS if h[2])
    assert drop_sites == EXPECTED_AUDIT_DROPPED_SITES
