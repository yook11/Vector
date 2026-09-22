"""例外の種類を判定し、各担当の変換関数へ振り分ける共通入口。"""

from pydantic import ValidationError
from sqlalchemy.exc import StatementError

from app.log_policy.exceptions.application import convert_application_error
from app.log_policy.exceptions.event_validation import (
    EVENT_VALIDATION_ERRORS,
    convert_event_validation_exception,
)
from app.log_policy.exceptions.sql import convert_sql_exception, is_postgres_error
from app.log_policy.exceptions.types import ConvertedException
from app.log_policy.exceptions.validation import convert_validation_exception
from app.shared.errors import ApplicationError


def convert_exception(exc: BaseException) -> ConvertedException:
    """専用の保護が必要な例外を優先し、種類ごとの変換結果を返す。"""
    if isinstance(exc, EVENT_VALIDATION_ERRORS):
        return convert_event_validation_exception(exc)
    if isinstance(exc, StatementError) or is_postgres_error(exc):
        return convert_sql_exception(exc)
    if isinstance(exc, ValidationError):
        return convert_validation_exception(exc)
    if isinstance(exc, ApplicationError):
        return convert_application_error(exc)
    return _convert_standard_exception(exc)


def _convert_standard_exception(exc: BaseException) -> ConvertedException:
    """通常例外はメッセージだけを取得し、任意属性を診断へ転記しない。"""
    try:
        return ConvertedException(message=str(exc))
    except Exception:
        return ConvertedException(message="[exception message unavailable]")
