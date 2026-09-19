"""SQLAlchemy 例外から、実データを除いた原因情報を取り出す。"""

from __future__ import annotations

import json
import re

from sqlalchemy.exc import StatementError

_SQL_DETAIL = re.compile(
    r"(?:^|\n)\s*(?:DETAIL|HINT|CONTEXT|QUERY|STATEMENT|LINE \d+):", re.I
)

PARAMETER_VISIT_LIMIT = 1000
PARAMETER_DEPTH_LIMIT = 10


def extract_sql_error_message(exc: StatementError) -> str:
    """SQL実データと補足文を除いた原因文を返す。"""
    message = exc.args[0] if exc.args and type(exc.args[0]) is str else "SQL error"
    # DETAIL などの補足文は実データを含むため、primary message だけを残す。
    message = _SQL_DETAIL.split(message, maxsplit=1)[0]

    # primary message にも bind 値が入るため、引数を安全な組み込み型だけ辿る。
    pending = [(exc.params, 0)]
    literals: set[str] = set()
    visited = 0
    while pending:
        value, depth = pending.pop()
        visited += 1
        if visited > PARAMETER_VISIT_LIMIT or depth > PARAMETER_DEPTH_LIMIT:
            return "SQL error (parameters exceeded inspection limit)"
        kind = type(value)
        if value is None:
            continue
        if kind is str:
            if value:
                # 原因文にエスケープされた形で入る値にも一致させる。
                literals.update((value, repr(value)[1:-1], json.dumps(value)[1:-1]))
        elif kind in {bool, int, float}:
            literals.add(str(value))
        elif kind in {dict, list, tuple}:
            if len(value) + len(pending) > PARAMETER_VISIT_LIMIT:
                return "SQL error (parameters exceeded inspection limit)"
            values = value.values() if kind is dict else value
            pending.extend((item, depth + 1) for item in values)
        else:
            return "SQL error (unsupported parameter type)"

    # 短い値が長い値の一部を先に置換しないよう、長い順に伏せる。
    for literal in sorted(literals, key=len, reverse=True):
        message = message.replace(literal, "***")
    return message


def extract_sqlstate(exc: StatementError) -> str | None:
    """driverの診断コードがSQLSTATEの形式を満たす場合だけ返す。"""
    state = getattr(exc.orig, "sqlstate", None) or getattr(exc.orig, "pgcode", None)
    return state if type(state) is str and re.fullmatch(r"[A-Z0-9]{5}", state) else None
