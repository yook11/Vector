"""SQLAlchemy 例外を DatabaseError へ落とす契約。"""

from __future__ import annotations

import pytest
from sqlalchemy.exc import (
    IntegrityError,
    InterfaceError,
    InvalidRequestError,
    OperationalError,
)
from sqlalchemy.exc import TimeoutError as SQLAlchemyTimeoutError

from app.db.errors import (
    DatabaseConnectionError,
    DatabaseConnectionErrorReason,
    DatabaseConstraintError,
    DatabaseConstraintErrorReason,
    DatabaseTimeoutError,
    DatabaseTimeoutErrorReason,
    DatabaseUnexpectedError,
)
from app.db.translate import translate_database_error


def _stmt_error(cls: type[Exception], **kwargs: object) -> Exception:
    return cls("SELECT 1", {}, Exception("orig"), **kwargs)


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
            DatabaseTimeoutErrorReason.STATEMENT_TIMEOUT,
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


def test_translate_database_error_maps_other_sqlalchemy_to_unexpected() -> None:
    translated = translate_database_error(InvalidRequestError("boom"))

    assert isinstance(translated, DatabaseUnexpectedError)
    assert translated.args == ()


def test_translate_does_not_treat_valid_interface_error_as_connection_lost() -> None:
    exc = _stmt_error(InterfaceError, connection_invalidated=False)

    translated = translate_database_error(exc)  # type: ignore[arg-type]

    assert isinstance(translated, DatabaseUnexpectedError)
