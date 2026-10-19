"""DBの失敗が再試行で解消しうるかの分類。DBに接続せず、変換後の例外を直接渡す。"""

from __future__ import annotations

import pytest

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
from app.db.retry_prospect import is_resolvable_by_retry


@pytest.mark.parametrize(
    "error",
    [
        DatabaseConnectionError(reason=DatabaseConnectionErrorReason.CONNECTION_LOST),
        DatabaseTimeoutError(reason=DatabaseTimeoutErrorReason.LOCK_TIMEOUT),
        DatabaseTimeoutError(reason=DatabaseTimeoutErrorReason.QUERY_CANCELED),
        DatabaseTimeoutError(reason=DatabaseTimeoutErrorReason.POOL_TIMEOUT),
        DatabaseTransactionConflictError(
            reason=DatabaseTransactionConflictErrorReason.DEADLOCK
        ),
        DatabaseTransactionConflictError(
            reason=DatabaseTransactionConflictErrorReason.SERIALIZATION_FAILURE
        ),
        DatabaseCachedStatementInvalidError(),
    ],
)
def test_connection_timeout_and_conflict_are_resolvable(error: DatabaseError) -> None:
    """接続・待ち・同時実行の衝突・文のキャッシュ失効は、やり直せば解消しうる失敗として分類する。"""
    assert is_resolvable_by_retry(error) is True


@pytest.mark.parametrize(
    "error",
    [
        DatabaseConstraintError(reason=DatabaseConstraintErrorReason.UNIQUE_VIOLATION),
        DatabaseConstraintError(
            reason=DatabaseConstraintErrorReason.INTEGRITY_CONSTRAINT_VIOLATION
        ),
        DatabaseSyntaxOrAccessError(),
        DatabaseDataError(),
        DatabaseUnexpectedError(),
    ],
)
def test_constraint_statement_data_and_unclassified_are_not_resolvable(
    error: DatabaseError,
) -> None:
    """制約違反・SQLや権限の誤り・値の誤り・分類できない失敗は、やり直しても解消しない失敗として分類する。"""
    assert is_resolvable_by_retry(error) is False
