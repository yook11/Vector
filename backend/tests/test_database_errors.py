"""共有データベースエラーの型と reason 契約。"""

from __future__ import annotations

import pytest

from app.database_errors import (
    DatabaseConnectionError,
    DatabaseConnectionErrorReason,
    DatabaseConstraintError,
    DatabaseConstraintErrorReason,
    DatabaseError,
    DatabaseTimeoutError,
    DatabaseTimeoutErrorReason,
    DatabaseUnexpectedError,
)
from app.logfire.exceptions import VectorDomainError


def test_database_error_is_vector_domain_error() -> None:
    assert issubclass(DatabaseError, VectorDomainError)
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
    ("exc", "expected"),
    [
        (
            DatabaseConnectionError(
                reason=DatabaseConnectionErrorReason.CONNECTION_FAILED
            ),
            "DatabaseConnectionError(reason='connection_failed')",
        ),
        (
            DatabaseConnectionError(
                reason=DatabaseConnectionErrorReason.CONNECTION_LOST
            ),
            "DatabaseConnectionError(reason='connection_lost')",
        ),
        (
            DatabaseTimeoutError(reason=DatabaseTimeoutErrorReason.LOCK_TIMEOUT),
            "DatabaseTimeoutError(reason='lock_timeout')",
        ),
        (
            DatabaseTimeoutError(reason=DatabaseTimeoutErrorReason.STATEMENT_TIMEOUT),
            "DatabaseTimeoutError(reason='statement_timeout')",
        ),
        (
            DatabaseConstraintError(
                reason=DatabaseConstraintErrorReason.UNIQUE_VIOLATION
            ),
            "DatabaseConstraintError(reason='unique_violation')",
        ),
        (
            DatabaseConstraintError(
                reason=DatabaseConstraintErrorReason.FOREIGN_KEY_VIOLATION
            ),
            "DatabaseConstraintError(reason='foreign_key_violation')",
        ),
        (
            DatabaseConstraintError(
                reason=DatabaseConstraintErrorReason.NOT_NULL_VIOLATION
            ),
            "DatabaseConstraintError(reason='not_null_violation')",
        ),
        (
            DatabaseConstraintError(
                reason=DatabaseConstraintErrorReason.CHECK_VIOLATION
            ),
            "DatabaseConstraintError(reason='check_violation')",
        ),
        (
            DatabaseConstraintError(
                reason=DatabaseConstraintErrorReason.UNCLASSIFIED_CONSTRAINT
            ),
            "DatabaseConstraintError(reason='unclassified_constraint')",
        ),
        (DatabaseUnexpectedError(), "DatabaseUnexpectedError"),
    ],
)
def test_str_exposes_class_and_reason_only(exc: DatabaseError, expected: str) -> None:
    assert str(exc) == expected
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
