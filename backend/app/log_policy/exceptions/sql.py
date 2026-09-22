"""SQLAlchemy 例外から、実データを除いた原因情報を取り出す。"""

from __future__ import annotations

import json
import re
from typing import Literal, NotRequired, TypedDict

from asyncpg import PostgresError
from sqlalchemy.dialects.postgresql.asyncpg import AsyncAdapt_asyncpg_dbapi
from sqlalchemy.exc import StatementError

from app.log_policy.exceptions.types import ConvertedException

_SQL_DETAIL = re.compile(
    r"(?:^|\n)\s*(?:DETAIL|HINT|CONTEXT|QUERY|STATEMENT|LINE \d+):", re.I
)

PARAMETER_VISIT_LIMIT = 1000
PARAMETER_DEPTH_LIMIT = 10
SQL_EXCEPTION_LIMIT = 8


class PostgresErrorDetails(TypedDict):
    """原因文と独立して保持するPostgreSQLの診断属性。"""

    kind: Literal["postgresql"]
    sqlstate: NotRequired[str]
    schema_name: NotRequired[str]
    table_name: NotRequired[str]
    column_name: NotRequired[str]
    constraint_name: NotRequired[str]
    data_type_name: NotRequired[str]


def convert_sql_exception(exc: BaseException) -> ConvertedException:
    """SQLの原因文を保護し、診断属性と原因の集約状態を共通形式へ渡す。"""
    try:
        message = (
            extract_sql_error_message(exc)
            if isinstance(exc, StatementError)
            else "[exception message omitted]"
        )
    except Exception:
        message = "[exception message unavailable]"

    # 原因文の取得に失敗しても、診断属性は独立して取得する。
    return ConvertedException(
        message=message,
        error_details=extract_sql_error_details(exc),
        cause_is_aggregated=True,
    )


def is_postgres_error(exc: BaseException) -> bool:
    """対応するドライバー型だけをPostgreSQLの例外として認める。"""
    return isinstance(exc, (PostgresError, AsyncAdapt_asyncpg_dbapi.Error))


def _read_attribute(exc: BaseException, name: str) -> object:
    """一属性の取得失敗で、他の診断属性を失わないようにする。"""
    try:
        return getattr(exc, name, None)
    except Exception:
        return None


def _postgres_source(exc: BaseException) -> BaseException | None:
    """SQLのラッパーを有限回辿り、診断属性を持つ元例外を優先する。"""
    current = _read_attribute(exc, "orig") if isinstance(exc, StatementError) else exc
    seen: set[int] = set()
    adapter: BaseException | None = None
    for _ in range(SQL_EXCEPTION_LIMIT):
        if not isinstance(current, BaseException) or id(current) in seen:
            break
        seen.add(id(current))
        if isinstance(current, PostgresError):
            return current
        if isinstance(current, AsyncAdapt_asyncpg_dbapi.Error) and adapter is None:
            adapter = current
        if isinstance(current, StatementError):
            current = _read_attribute(current, "orig")
            continue
        cause = _read_attribute(current, "__cause__")
        if isinstance(cause, BaseException):
            current = cause
        elif _read_attribute(current, "__suppress_context__") is False:
            current = _read_attribute(current, "__context__")
        else:
            break
    return adapter


def extract_sql_error_details(exc: BaseException) -> PostgresErrorDetails | None:
    """同じ元例外の許可属性だけを取り、文字列の保護は共通の値準備へ渡す。"""
    source = _postgres_source(exc)
    if source is None:
        return None
    details: PostgresErrorDetails = {"kind": "postgresql"}
    for attribute in ("sqlstate", "pgcode"):
        state = _read_attribute(source, attribute)
        if type(state) is str and re.fullmatch(r"[A-Z0-9]{5}", state):
            details["sqlstate"] = state
            break
    for name in (
        "schema_name",
        "table_name",
        "column_name",
        "constraint_name",
        "data_type_name",
    ):
        value = _read_attribute(source, name)
        if type(value) is str and value:
            details[name] = value
    return details if len(details) > 1 else None


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
