"""データベース由来の共有エラー。SQL や params は例外に載せない。"""

from __future__ import annotations

from enum import StrEnum

__all__ = [
    "DatabaseConnectionError",
    "DatabaseConnectionErrorReason",
    "DatabaseConstraintError",
    "DatabaseConstraintErrorReason",
    "DatabaseError",
    "DatabaseTimeoutError",
    "DatabaseTimeoutErrorReason",
    "DatabaseUnexpectedError",
]


class DatabaseConnectionErrorReason(StrEnum):
    CONNECTION_FAILED = "connection_failed"
    CONNECTION_LOST = "connection_lost"


class DatabaseTimeoutErrorReason(StrEnum):
    LOCK_TIMEOUT = "lock_timeout"
    STATEMENT_TIMEOUT = "statement_timeout"


class DatabaseConstraintErrorReason(StrEnum):
    UNIQUE_VIOLATION = "unique_violation"
    FOREIGN_KEY_VIOLATION = "foreign_key_violation"
    NOT_NULL_VIOLATION = "not_null_violation"
    CHECK_VIOLATION = "check_violation"
    UNCLASSIFIED_CONSTRAINT = "unclassified_constraint"


class DatabaseError(Exception):
    """データベース由来エラーの共通祖先。捕捉用であり、直接は送出しない。"""

    def __init__(self) -> None:
        if type(self) is DatabaseError:
            raise TypeError("DatabaseError cannot be instantiated directly")
        super().__init__()


class DatabaseConnectionError(DatabaseError):
    """接続できなかった、または接続が切れた。"""

    reason: DatabaseConnectionErrorReason

    def __init__(self, *, reason: DatabaseConnectionErrorReason) -> None:
        if not isinstance(reason, DatabaseConnectionErrorReason):
            raise TypeError("reason must be a DatabaseConnectionErrorReason")
        super().__init__()
        self.reason = reason


class DatabaseTimeoutError(DatabaseError):
    """データベースの待ちが尽きた。"""

    reason: DatabaseTimeoutErrorReason

    def __init__(self, *, reason: DatabaseTimeoutErrorReason) -> None:
        if not isinstance(reason, DatabaseTimeoutErrorReason):
            raise TypeError("reason must be a DatabaseTimeoutErrorReason")
        super().__init__()
        self.reason = reason


class DatabaseConstraintError(DatabaseError):
    """データベースの制約違反が起きた。"""

    reason: DatabaseConstraintErrorReason

    def __init__(self, *, reason: DatabaseConstraintErrorReason) -> None:
        if not isinstance(reason, DatabaseConstraintErrorReason):
            raise TypeError("reason must be a DatabaseConstraintErrorReason")
        super().__init__()
        self.reason = reason


class DatabaseUnexpectedError(DatabaseError):
    """接続・タイムアウト・制約のどれにも分類できないデータベースエラー。"""
