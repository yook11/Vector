"""例外の種類に固有の入力値を除き、原因文と診断属性へ変換する。"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal, TypedDict

from pydantic import ValidationError
from sqlalchemy.exc import StatementError

from app.log_policy.exceptions.sql import (
    PostgresErrorDetails,
    extract_sql_error_details,
    extract_sql_error_message,
    is_postgres_error,
)
from app.log_policy.exceptions.validation import extract_validation_message


class ApplicationValidationIssue(TypedDict):
    """検証境界で分類済みの項目と違反コード。"""

    field: str
    code: str


class ApplicationValidationDetails(TypedDict):
    """アプリの検証例外から取得する診断情報。"""

    kind: Literal["application_validation"]
    reason: str
    issues: list[ApplicationValidationIssue]


type ErrorDetails = PostgresErrorDetails | ApplicationValidationDetails


@dataclass(frozen=True)
class ConvertedException:
    """例外1件の変換結果と、内部原因の集約状態。"""

    message: str
    error_details: ErrorDetails | None = None
    cause_is_aggregated: bool = False


type ExceptionConverter = Callable[[BaseException], ConvertedException]


def convert_exception(exc: BaseException) -> ConvertedException:
    """例外の種類に応じて、原因文と診断属性を取り出す。"""
    sql_exception = isinstance(exc, StatementError) or is_postgres_error(exc)
    try:
        if isinstance(exc, StatementError):
            message = extract_sql_error_message(exc)
        elif is_postgres_error(exc):
            message = "[exception message omitted]"
        elif isinstance(exc, ValidationError):
            message = extract_validation_message(exc)
        else:
            message = str(exc)
    except Exception:
        message = "[exception message unavailable]"

    # 原因文の取得に失敗しても、診断属性は独立して取得する。
    details = extract_sql_error_details(exc) if sql_exception else None
    return ConvertedException(
        message=message,
        error_details=details,
        cause_is_aggregated=sql_exception,
    )
