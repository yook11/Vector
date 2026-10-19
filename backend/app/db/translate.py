"""SQLAlchemy 例外を共有 DatabaseError へ落とす。SQL / params は載せない。"""

from __future__ import annotations

from sqlalchemy.dialects.postgresql.asyncpg import AsyncAdapt_asyncpg_dbapi
from sqlalchemy.exc import (
    DBAPIError,
    IntegrityError,
    InterfaceError,
    OperationalError,
    SQLAlchemyError,
)
from sqlalchemy.exc import TimeoutError as SQLAlchemyTimeoutError

from app.db.errors import (
    DatabaseCachedStatementInvalidError,
    DatabaseConnectionError,
    DatabaseConnectionErrorReason,
    DatabaseConstraintError,
    DatabaseConstraintErrorReason,
    DatabaseDataError,
    DatabaseError,
    DatabaseSyntaxOrAccessError,
    DatabaseTimeoutError,
    DatabaseTimeoutErrorReason,
    DatabaseTransactionConflictError,
    DatabaseTransactionConflictErrorReason,
    DatabaseUnexpectedError,
)

_CONSTRAINT_REASONS = {
    "23505": DatabaseConstraintErrorReason.UNIQUE_VIOLATION,
    "23503": DatabaseConstraintErrorReason.FOREIGN_KEY_VIOLATION,
    "23502": DatabaseConstraintErrorReason.NOT_NULL_VIOLATION,
    "23514": DatabaseConstraintErrorReason.CHECK_VIOLATION,
    "23000": DatabaseConstraintErrorReason.INTEGRITY_CONSTRAINT_VIOLATION,
}


def translate_database_error(exc: SQLAlchemyError) -> DatabaseError:
    """SQLAlchemy 例外を対応する DatabaseError にする。"""
    if isinstance(exc, SQLAlchemyTimeoutError):
        return DatabaseTimeoutError(reason=DatabaseTimeoutErrorReason.POOL_TIMEOUT)
    if isinstance(exc, InterfaceError) and exc.connection_invalidated:
        return DatabaseConnectionError(
            reason=DatabaseConnectionErrorReason.CONNECTION_LOST
        )
    # キャッシュを捨てて回復する例外だが、同じ 0A000 には回復しない未対応機能もある。
    if isinstance(exc, DBAPIError) and isinstance(
        exc.orig, AsyncAdapt_asyncpg_dbapi.InvalidCachedStatementError
    ):
        return DatabaseCachedStatementInvalidError()
    # asyncpg は deadlock や timeout も汎用の DBAPIError にするため、SQLSTATE で分ける。
    sqlstate = (
        getattr(exc.orig, "sqlstate", None) if isinstance(exc, DBAPIError) else None
    )
    if isinstance(sqlstate, str):
        translated = _translate_sqlstate(sqlstate)
        if translated is not None:
            return translated
    if isinstance(exc, IntegrityError):
        return DatabaseConstraintError(
            reason=DatabaseConstraintErrorReason.UNCLASSIFIED_CONSTRAINT
        )
    if isinstance(exc, OperationalError):
        return DatabaseConnectionError(
            reason=DatabaseConnectionErrorReason.CONNECTION_FAILED
        )
    return DatabaseUnexpectedError()


def _translate_sqlstate(sqlstate: str) -> DatabaseError | None:
    match sqlstate:
        case "55P03":
            return DatabaseTimeoutError(reason=DatabaseTimeoutErrorReason.LOCK_TIMEOUT)
        case "57014":
            return DatabaseTimeoutError(
                reason=DatabaseTimeoutErrorReason.QUERY_CANCELED
            )
        case "40P01":
            return DatabaseTransactionConflictError(
                reason=DatabaseTransactionConflictErrorReason.DEADLOCK
            )
        case "40001":
            return DatabaseTransactionConflictError(
                reason=DatabaseTransactionConflictErrorReason.SERIALIZATION_FAILURE
            )
    match sqlstate[:2]:
        case "23":
            return DatabaseConstraintError(
                reason=_CONSTRAINT_REASONS.get(
                    sqlstate, DatabaseConstraintErrorReason.UNCLASSIFIED_CONSTRAINT
                )
            )
        case "08":
            return DatabaseConnectionError(
                reason=DatabaseConnectionErrorReason.CONNECTION_FAILED
            )
        case "42":
            return DatabaseSyntaxOrAccessError()
        case "22":
            return DatabaseDataError()
    return None
