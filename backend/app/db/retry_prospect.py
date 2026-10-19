"""DBの失敗が再試行で解消しうるかを、起きたことの種類から返す。"""

from __future__ import annotations

from app.db.errors import (
    DatabaseCachedStatementInvalidError,
    DatabaseConnectionError,
    DatabaseError,
    DatabaseTimeoutError,
    DatabaseTransactionConflictError,
)


def is_resolvable_by_retry(error: DatabaseError) -> bool:
    """同じトランザクションをやり直すだけで解消しうるDBの失敗かを返す。"""
    match error:
        case (
            DatabaseConnectionError()
            | DatabaseTimeoutError()
            | DatabaseTransactionConflictError()
            | DatabaseCachedStatementInvalidError()
        ):
            return True
        # 制約違反・SQLや値の誤り・分類不能は、コードかデータを直すまで変わらない。
        case _:
            return False
