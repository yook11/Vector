"""SQLAlchemy 例外を DatabaseError へ落とす契約。sqlstate を持つ元例外は模造する。"""

from __future__ import annotations

import pytest
from sqlalchemy.dialects.postgresql.asyncpg import AsyncAdapt_asyncpg_dbapi
from sqlalchemy.exc import (
    DBAPIError,
    IntegrityError,
    InterfaceError,
    InternalError,
    InvalidRequestError,
    NotSupportedError,
    OperationalError,
    ProgrammingError,
)
from sqlalchemy.exc import TimeoutError as SQLAlchemyTimeoutError

from app.db.errors import (
    DatabaseCachedStatementInvalidError,
    DatabaseConnectionError,
    DatabaseConnectionErrorReason,
    DatabaseConstraintError,
    DatabaseConstraintErrorReason,
    DatabaseDataError,
    DatabaseSyntaxOrAccessError,
    DatabaseTimeoutError,
    DatabaseTimeoutErrorReason,
    DatabaseTransactionConflictError,
    DatabaseTransactionConflictErrorReason,
    DatabaseUnexpectedError,
)
from app.db.translate import translate_database_error


class _AdapterError(Exception):
    """asyncpg のアダプターが元例外に sqlstate を付けた形を模す。"""

    def __init__(self, sqlstate: str) -> None:
        super().__init__("adapter error")
        self.sqlstate = sqlstate


def _stmt_error(
    cls: type[Exception], sqlstate: str | None = None, **kwargs: object
) -> Exception:
    orig = Exception("orig") if sqlstate is None else _AdapterError(sqlstate)
    return cls("SELECT 1", {}, orig, **kwargs)


@pytest.mark.parametrize(
    ("exc", "expected_type", "expected_reason"),
    [
        (
            _stmt_error(IntegrityError),
            DatabaseConstraintError,
            DatabaseConstraintErrorReason.UNCLASSIFIED_CONSTRAINT,
        ),
        (
            SQLAlchemyTimeoutError("pool timed out"),
            DatabaseTimeoutError,
            DatabaseTimeoutErrorReason.POOL_TIMEOUT,
        ),
        (
            _stmt_error(OperationalError),
            DatabaseConnectionError,
            DatabaseConnectionErrorReason.CONNECTION_FAILED,
        ),
        (
            _stmt_error(InterfaceError, connection_invalidated=True),
            DatabaseConnectionError,
            DatabaseConnectionErrorReason.CONNECTION_LOST,
        ),
    ],
)
def test_translate_database_error_maps_known_sqlalchemy_exceptions(
    exc: Exception,
    expected_type: type[object],
    expected_reason: object,
) -> None:
    translated = translate_database_error(exc)  # type: ignore[arg-type]

    assert isinstance(translated, expected_type)
    assert translated.reason == expected_reason
    assert translated.args == ()


@pytest.mark.parametrize(
    ("exc", "expected_type", "expected_reason"),
    [
        pytest.param(
            _stmt_error(DBAPIError, "55P03"),
            DatabaseTimeoutError,
            DatabaseTimeoutErrorReason.LOCK_TIMEOUT,
            id="lock_not_available",
        ),
        pytest.param(
            _stmt_error(DBAPIError, "57014"),
            DatabaseTimeoutError,
            DatabaseTimeoutErrorReason.QUERY_CANCELED,
            id="query_canceled",
        ),
        pytest.param(
            _stmt_error(DBAPIError, "40P01"),
            DatabaseTransactionConflictError,
            DatabaseTransactionConflictErrorReason.DEADLOCK,
            id="deadlock_detected",
        ),
        pytest.param(
            _stmt_error(DBAPIError, "40001"),
            DatabaseTransactionConflictError,
            DatabaseTransactionConflictErrorReason.SERIALIZATION_FAILURE,
            id="serialization_failure",
        ),
        pytest.param(
            _stmt_error(IntegrityError, "23505"),
            DatabaseConstraintError,
            DatabaseConstraintErrorReason.UNIQUE_VIOLATION,
            id="unique_violation",
        ),
        pytest.param(
            _stmt_error(IntegrityError, "23503"),
            DatabaseConstraintError,
            DatabaseConstraintErrorReason.FOREIGN_KEY_VIOLATION,
            id="foreign_key_violation",
        ),
        pytest.param(
            _stmt_error(IntegrityError, "23502"),
            DatabaseConstraintError,
            DatabaseConstraintErrorReason.NOT_NULL_VIOLATION,
            id="not_null_violation",
        ),
        pytest.param(
            _stmt_error(IntegrityError, "23514"),
            DatabaseConstraintError,
            DatabaseConstraintErrorReason.CHECK_VIOLATION,
            id="check_violation",
        ),
        pytest.param(
            _stmt_error(IntegrityError, "23000"),
            DatabaseConstraintError,
            DatabaseConstraintErrorReason.INTEGRITY_CONSTRAINT_VIOLATION,
            id="integrity_constraint_violation",
        ),
        pytest.param(
            _stmt_error(IntegrityError, "23P01"),
            DatabaseConstraintError,
            DatabaseConstraintErrorReason.UNCLASSIFIED_CONSTRAINT,
            id="exclusion_violation",
        ),
        pytest.param(
            _stmt_error(DBAPIError, "08006"),
            DatabaseConnectionError,
            DatabaseConnectionErrorReason.CONNECTION_FAILED,
            id="connection_failure",
        ),
    ],
)
def test_translate_maps_sqlstate_to_what_happened(
    exc: Exception,
    expected_type: type[object],
    expected_reason: object,
) -> None:
    """SQLAlchemy の例外クラスによらず、sqlstate から種類と理由へ変換する。"""
    translated = translate_database_error(exc)  # type: ignore[arg-type]

    assert type(translated) is expected_type
    assert getattr(translated, "reason", None) == expected_reason


@pytest.mark.parametrize(
    ("exc", "expected_type"),
    [
        pytest.param(
            _stmt_error(ProgrammingError, "42501"),
            DatabaseSyntaxOrAccessError,
            id="insufficient_privilege",
        ),
        pytest.param(
            _stmt_error(ProgrammingError, "42703"),
            DatabaseSyntaxOrAccessError,
            id="undefined_column",
        ),
        pytest.param(
            _stmt_error(DBAPIError, "22012"),
            DatabaseDataError,
            id="division_by_zero",
        ),
        pytest.param(
            _stmt_error(InternalError, "XX000"),
            DatabaseUnexpectedError,
            id="internal_error",
        ),
    ],
)
def test_translate_maps_reasonless_sqlstate_classes(
    exc: Exception, expected_type: type[object]
) -> None:
    """42系をSQLの誤り、22系を値の誤り、未対応の sqlstate を分類不能へ変換する。"""
    translated = translate_database_error(exc)  # type: ignore[arg-type]

    assert type(translated) is expected_type


def test_translate_maps_invalid_cached_statement_to_cache_invalidation() -> None:
    """文のキャッシュ失効を、同じ 0A000 の未対応機能と分けて変換する。"""
    orig = AsyncAdapt_asyncpg_dbapi.InvalidCachedStatementError(
        "cached plan must not change result type"
    )
    orig.sqlstate = "0A000"  # type: ignore[attr-defined]
    exc = NotSupportedError("SELECT 1", {}, orig)

    assert type(translate_database_error(exc)) is DatabaseCachedStatementInvalidError


def test_translate_keeps_other_feature_not_supported_unexpected() -> None:
    """キャッシュ失効でない 0A000 は、解消しうる失敗にせず分類不能へ変換する。"""
    exc = _stmt_error(NotSupportedError, "0A000")

    translated = translate_database_error(exc)  # type: ignore[arg-type]

    assert type(translated) is DatabaseUnexpectedError


def test_translate_database_error_maps_other_sqlalchemy_to_unexpected() -> None:
    translated = translate_database_error(InvalidRequestError("boom"))

    assert isinstance(translated, DatabaseUnexpectedError)
    assert translated.args == ()


def test_translate_does_not_treat_valid_interface_error_as_connection_lost() -> None:
    exc = _stmt_error(InterfaceError, connection_invalidated=False)

    translated = translate_database_error(exc)  # type: ignore[arg-type]

    assert isinstance(translated, DatabaseUnexpectedError)
