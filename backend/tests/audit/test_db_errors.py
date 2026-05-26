"""``app.audit.db_errors.classify_db_error`` の単体テスト (unit, DB 不要)。

検証する不変条件:
- SQLAlchemy 例外を isinstance で意味ラベル (code, cause) に分類する
- 分類は ``exc.code`` の有無に依存しない。SQLAlchemy が振るドキュメント参照
  コード (``IntegrityError.code="gkpj"`` 等) を拾わず、固定の ``db_*_error`` を返す
- 非 DB 例外は ``.code`` を持っていても ``None`` を返す (helper は ``.code`` を
  読まない。``.code`` を信用するのは各 stage の自前 marker 側の責務)
"""

from __future__ import annotations

import pytest
from sqlalchemy.exc import (
    DataError,
    IntegrityError,
    InvalidRequestError,
    OperationalError,
    ProgrammingError,
)

from app.audit.db_errors import (
    DbErrorCause,
    DbErrorClassification,
    classify_db_error,
)


def _stmt_error(cls: type[Exception]) -> Exception:
    """``StatementError`` 系 (``statement, params, orig``) を生成する。"""
    return cls("SELECT 1", {}, Exception("orig"))


class _CodedNonDbError(Exception):
    """``.code`` を持つが SQLAlchemy 例外でない非 DB 例外 (AI marker 模倣)。"""

    code = "ai_error_network"


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (
            _stmt_error(OperationalError),
            DbErrorClassification("db_runtime_error", DbErrorCause.RUNTIME),
        ),
        (
            _stmt_error(IntegrityError),
            DbErrorClassification("db_constraint_error", DbErrorCause.CONSTRAINT),
        ),
        (
            _stmt_error(ProgrammingError),
            DbErrorClassification(
                "db_query_or_schema_error", DbErrorCause.QUERY_OR_SCHEMA
            ),
        ),
        (
            _stmt_error(DataError),
            DbErrorClassification(
                "db_query_or_schema_error", DbErrorCause.QUERY_OR_SCHEMA
            ),
        ),
        # SQLAlchemyError 直系だが上記いずれにも該当しない → 総括 UNKNOWN。
        (
            InvalidRequestError("boom"),
            DbErrorClassification("db_unknown_error", DbErrorCause.UNKNOWN),
        ),
    ],
)
def test_classify_db_error_maps_sqlalchemy_exceptions(
    exc: Exception, expected: DbErrorClassification
) -> None:
    """SQLAlchemy 例外が固定の (code, cause) に分類される (``.code`` 非依存)。"""
    assert classify_db_error(exc) == expected


@pytest.mark.parametrize(
    "exc",
    [
        RuntimeError("boom"),  # .code を持たない非 DB 例外
        _CodedNonDbError("boom"),  # .code を持つが SQLAlchemy 例外でない
    ],
)
def test_classify_db_error_returns_none_for_non_db_exceptions(exc: Exception) -> None:
    """非 DB 例外は ``.code`` の有無に関わらず ``None`` (``.code`` 非依存)。"""
    assert classify_db_error(exc) is None
