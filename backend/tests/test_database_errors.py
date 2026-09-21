"""共有データベースエラーの型と reason 契約。"""

from __future__ import annotations

import pytest

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


def test_database_error_directly_inherits_exception() -> None:
    assert DatabaseError.__bases__ == (Exception,)
    assert issubclass(DatabaseConnectionError, DatabaseError)
    assert issubclass(DatabaseTimeoutError, DatabaseError)
    assert issubclass(DatabaseConstraintError, DatabaseError)
    assert issubclass(DatabaseUnexpectedError, DatabaseError)


def test_database_error_cannot_be_instantiated_directly() -> None:
    with pytest.raises(TypeError, match="cannot be instantiated directly"):
        DatabaseError()


def test_reason_values_name_what_happened() -> None:
    assert {member.value for member in DatabaseConnectionErrorReason} == {
        "connection_failed",
        "connection_lost",
    }
    assert {member.value for member in DatabaseTimeoutErrorReason} == {
        "lock_timeout",
        "statement_timeout",
    }
    assert {member.value for member in DatabaseConstraintErrorReason} == {
        "unique_violation",
        "foreign_key_violation",
        "not_null_violation",
        "check_violation",
        "unclassified_constraint",
    }


@pytest.mark.parametrize(
    "exc",
    [
        DatabaseConnectionError(reason=DatabaseConnectionErrorReason.CONNECTION_FAILED),
        DatabaseConnectionError(reason=DatabaseConnectionErrorReason.CONNECTION_LOST),
        DatabaseTimeoutError(reason=DatabaseTimeoutErrorReason.LOCK_TIMEOUT),
        DatabaseTimeoutError(reason=DatabaseTimeoutErrorReason.STATEMENT_TIMEOUT),
        DatabaseConstraintError(reason=DatabaseConstraintErrorReason.UNIQUE_VIOLATION),
        DatabaseConstraintError(
            reason=DatabaseConstraintErrorReason.FOREIGN_KEY_VIOLATION
        ),
        DatabaseConstraintError(
            reason=DatabaseConstraintErrorReason.NOT_NULL_VIOLATION
        ),
        DatabaseConstraintError(reason=DatabaseConstraintErrorReason.CHECK_VIOLATION),
        DatabaseConstraintError(
            reason=DatabaseConstraintErrorReason.UNCLASSIFIED_CONSTRAINT
        ),
        DatabaseUnexpectedError(),
    ],
)
def test_database_error_uses_standard_empty_message(exc: DatabaseError) -> None:
    """メッセージ未指定時は型名や理由で補完しない。"""
    assert str(exc) == ""
    assert exc.args == ()


@pytest.mark.parametrize(
    "cls",
    [DatabaseConnectionError, DatabaseTimeoutError, DatabaseConstraintError],
)
def test_reasoned_errors_reject_non_enum_reason(cls: type[DatabaseError]) -> None:
    with pytest.raises(TypeError, match="reason must be"):
        cls(reason="connection_failed")  # type: ignore[call-arg, arg-type]
    with pytest.raises(TypeError):
        cls("SELECT 1")  # type: ignore[misc]
