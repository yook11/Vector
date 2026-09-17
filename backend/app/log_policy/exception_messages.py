"""例外の種類に応じて、入力値を除いた原因情報を抽出する。"""

from __future__ import annotations

import json
import re
from typing import get_args

from pydantic import ValidationError
from pydantic_core import ErrorType
from sqlalchemy.exc import StatementError

_SQL_DETAIL = re.compile(
    r"(?:^|\n)\s*(?:DETAIL|HINT|CONTEXT|QUERY|STATEMENT|LINE \d+):", re.I
)
_VALIDATION_TYPES = frozenset(get_args(ErrorType))


def extract_sql_error_message(exc: StatementError) -> str:
    """SQL実データと補足文を除き、原因文を取り出す。"""
    message = exc.args[0] if exc.args and type(exc.args[0]) is str else "SQL error"
    message = _SQL_DETAIL.split(message, maxsplit=1)[0]
    # primary message にも bind 値が入るため、引数を安全な組み込み型だけ辿る。
    pending = [(exc.params, 0)]
    literals: set[str] = set()
    visited = 0
    while pending:
        value, depth = pending.pop()
        visited += 1
        if visited > 1000 or depth > 10:
            return "SQL error (parameters exceeded inspection limit)"
        kind = type(value)
        if value is None:
            continue
        if kind is str:
            if value:
                literals.update((value, repr(value)[1:-1], json.dumps(value)[1:-1]))
        elif kind in {bool, int, float}:
            literals.add(str(value))
        elif kind in {dict, list, tuple}:
            if len(value) + len(pending) > 1000:
                return "SQL error (parameters exceeded inspection limit)"
            values = value.values() if kind is dict else value
            pending.extend((item, depth + 1) for item in values)
        else:
            return "SQL error (unsupported parameter type)"
    for literal in sorted(literals, key=len, reverse=True):
        message = message.replace(literal, "***")
    return message


def extract_validation_error_summary(exc: ValidationError) -> str:
    """入力値を含む説明や位置を除き、検証件数と標準分類を取り出す。"""
    if exc.error_count() > 100:
        return f"Validation failed ({exc.error_count()} errors)"
    # msg / loc / title / custom type にも入力が入り得るため標準分類だけ残す。
    errors = exc.errors(include_input=False, include_context=False, include_url=False)
    kinds = sorted(
        {
            e["type"] if e["type"] in _VALIDATION_TYPES else "custom_error"
            for e in errors
        }
    )
    return f"Validation failed ({exc.error_count()} errors): {', '.join(kinds)}"


def extract_exception_message(exc: BaseException) -> str:
    """対象固有の抽出に失敗した場合は原文へ戻らず固定文を返す。"""
    try:
        if isinstance(exc, StatementError):
            return extract_sql_error_message(exc)
        if isinstance(exc, ValidationError):
            return extract_validation_error_summary(exc)
        return str(exc)
    except Exception:
        return "[exception message unavailable]"


def extract_sqlstate(exc: BaseException) -> str | None:
    """SQL例外から形式を満たす診断コードだけを取り出す。"""
    if not isinstance(exc, StatementError):
        return None
    state = getattr(exc.orig, "sqlstate", None) or getattr(exc.orig, "pgcode", None)
    return state if type(state) is str and re.fullmatch(r"[A-Z0-9]{5}", state) else None
