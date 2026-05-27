"""SQLAlchemy 例外を監査ラベルに分類する pure helper。

``error_chain.py`` と同じ「session も I/O も持たない純粋関数」哲学のモジュール。
分析トリオ 3 stage (curation / assessment / embedding) の audit repository が
DB 例外を ``outcome_code`` / ``retryability`` / ``failure_kind`` へ投影するときに
共通で使う。

背景: 各 stage の ``_code_of`` は元々 ``getattr(exc, "code", None)`` で code を
読んでいたが、これは ``.code`` を持つ任意の例外を無条件に信用するため、SQLAlchemy
が振るドキュメント参照コード (``IntegrityError.code="gkpj"``) を拾ってしまう。
``ProgrammingError`` は ``.code`` を持たず ``unexpected_error`` に潰れる。本 helper
は SQLAlchemy 例外を明示 isinstance で分類し、queryable な event code / ``cause``
のペアを返す (``.code`` を信用するのは各 stage の自前 marker だけにする方針)。

``code`` は ``pipeline_events.outcome_code`` に入る event code の元値。``cause``
enum のリネームから独立に固定する (機械導出せず dataclass で明示ペア)。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from sqlalchemy.exc import (
    DataError,
    IntegrityError,
    OperationalError,
    ProgrammingError,
    SQLAlchemyError,
)


class DbErrorCause(StrEnum):
    """DB 例外の性質。failure projection への変換に使う中間表現。"""

    RUNTIME = "runtime"  # 接続断 / deadlock / lock timeout / 一時障害
    CONSTRAINT = "constraint"  # unique / FK / NOT NULL 等の制約違反
    QUERY_OR_SCHEMA = "query_or_schema"  # SQL / カラム不在 / 型・値不整合 / migration
    UNKNOWN = "unknown"  # 未分類 SQLAlchemy 例外


@dataclass(frozen=True, slots=True)
class DbErrorClassification:
    """DB 例外の分類結果。

    Attributes:
        code: ``outcome_code`` に焼く集計ラベル (wire 値、enum 非依存で固定)。
        cause: DB 例外の性質。各 stage が retryability/failure_kind へ変換する軸。
    """

    code: str
    cause: DbErrorCause


def classify_db_error(exc: BaseException) -> DbErrorClassification | None:
    """SQLAlchemy 例外を監査ラベルに分類する。DB 例外でなければ ``None``。

    ``OperationalError`` / ``IntegrityError`` / ``ProgrammingError`` / ``DataError``
    は ``DBAPIError`` の兄弟サブクラスで互いに独立 (順序非依存)。``SQLAlchemyError``
    は全 DB 例外の基底なので未分類の総括として必ず最後に置く。
    """
    if isinstance(exc, OperationalError):
        # 接続断・deadlock・lock timeout・一時障害は再試行で回復しうる。
        return DbErrorClassification("db_runtime_error", DbErrorCause.RUNTIME)
    if isinstance(exc, IntegrityError):
        # unique / FK / NOT NULL 違反。retry しても同じ結果。
        return DbErrorClassification("db_constraint_error", DbErrorCause.CONSTRAINT)
    if isinstance(exc, (ProgrammingError, DataError)):
        # SQL / カラム不在 / 型・値不整合 / migration。人間の調査が要る。
        return DbErrorClassification(
            "db_query_or_schema_error", DbErrorCause.QUERY_OR_SCHEMA
        )
    if isinstance(exc, SQLAlchemyError):
        # 未分類 DB 例外の総括。error_class 列で個別に調査する。
        return DbErrorClassification("db_unknown_error", DbErrorCause.UNKNOWN)
    return None
