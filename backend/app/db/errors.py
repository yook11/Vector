"""データベース由来の共有エラー。SQL や params は例外に載せない。"""

from __future__ import annotations

from enum import StrEnum

__all__ = [
    "DatabaseCachedStatementInvalidError",
    "DatabaseConnectionError",
    "DatabaseConnectionErrorReason",
    "DatabaseConstraintError",
    "DatabaseConstraintErrorReason",
    "DatabaseDataError",
    "DatabaseError",
    "DatabaseSyntaxOrAccessError",
    "DatabaseTimeoutError",
    "DatabaseTimeoutErrorReason",
    "DatabaseTransactionConflictError",
    "DatabaseTransactionConflictErrorReason",
    "DatabaseUnexpectedError",
]


class DatabaseConnectionErrorReason(StrEnum):
    CONNECTION_FAILED = "connection_failed"
    CONNECTION_LOST = "connection_lost"


class DatabaseTimeoutErrorReason(StrEnum):
    LOCK_TIMEOUT = "lock_timeout"
    # statement_timeout による取り消しと、外からの取り消しを区別できない。
    QUERY_CANCELED = "query_canceled"
    POOL_TIMEOUT = "pool_timeout"


class DatabaseTransactionConflictErrorReason(StrEnum):
    DEADLOCK = "deadlock"
    SERIALIZATION_FAILURE = "serialization_failure"


class DatabaseConstraintErrorReason(StrEnum):
    UNIQUE_VIOLATION = "unique_violation"
    FOREIGN_KEY_VIOLATION = "foreign_key_violation"
    NOT_NULL_VIOLATION = "not_null_violation"
    CHECK_VIOLATION = "check_violation"
    # 制約を特定しない汎用の違反で、Vector では排他トリガーが出す。
    INTEGRITY_CONSTRAINT_VIOLATION = "integrity_constraint_violation"
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
    """データベースの待ちが尽きた、または実行中の文が取り消された。"""

    reason: DatabaseTimeoutErrorReason

    def __init__(self, *, reason: DatabaseTimeoutErrorReason) -> None:
        if not isinstance(reason, DatabaseTimeoutErrorReason):
            raise TypeError("reason must be a DatabaseTimeoutErrorReason")
        super().__init__()
        self.reason = reason


class DatabaseTransactionConflictError(DatabaseError):
    """同時に走ったトランザクションとぶつかり、こちらのトランザクションが取り消された。"""

    reason: DatabaseTransactionConflictErrorReason

    def __init__(self, *, reason: DatabaseTransactionConflictErrorReason) -> None:
        if not isinstance(reason, DatabaseTransactionConflictErrorReason):
            raise TypeError("reason must be a DatabaseTransactionConflictErrorReason")
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


class DatabaseCachedStatementInvalidError(DatabaseError):
    """接続に保存していた準備済みの文が、ほかの接続のスキーマ変更で使えなくなった。"""


class DatabaseSyntaxOrAccessError(DatabaseError):
    """SQL の誤り、存在しないオブジェクトの参照、権限の不足が起きた。"""


class DatabaseDataError(DatabaseError):
    """SQL で扱った値が不正だった。"""


class DatabaseUnexpectedError(DatabaseError):
    """どの種類にも分類できないデータベースエラー。"""
