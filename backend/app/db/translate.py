"""SQLAlchemy 例外を共有 DatabaseError へ落とす。SQL / params は載せない。"""

from __future__ import annotations

from sqlalchemy.exc import (
    IntegrityError,
    InterfaceError,
    OperationalError,
    SQLAlchemyError,
)
from sqlalchemy.exc import TimeoutError as SQLAlchemyTimeoutError

from app.db.errors import (
    DatabaseConnectionError,
    DatabaseConnectionErrorReason,
    DatabaseConstraintError,
    DatabaseConstraintErrorReason,
    DatabaseError,
    DatabaseTimeoutError,
    DatabaseTimeoutErrorReason,
    DatabaseUnexpectedError,
)


def translate_database_error(exc: SQLAlchemyError) -> DatabaseError:
    """SQLAlchemy 例外を対応する DatabaseError にする。"""
    if isinstance(exc, IntegrityError):
        return DatabaseConstraintError(
            reason=DatabaseConstraintErrorReason.UNCLASSIFIED_CONSTRAINT
        )
    if isinstance(exc, SQLAlchemyTimeoutError):
        return DatabaseTimeoutError(reason=DatabaseTimeoutErrorReason.STATEMENT_TIMEOUT)
    if isinstance(exc, OperationalError):
        return DatabaseConnectionError(
            reason=DatabaseConnectionErrorReason.CONNECTION_FAILED
        )
    if isinstance(exc, InterfaceError) and exc.connection_invalidated:
        return DatabaseConnectionError(
            reason=DatabaseConnectionErrorReason.CONNECTION_LOST
        )
    return DatabaseUnexpectedError()
