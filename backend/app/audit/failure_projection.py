"""失敗属性 projection の内部表現。

失敗の意味論は stage error class の ClassVar と本 module の projection に集約する。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TypedDict

from app.audit.db_errors import DbErrorCause, classify_db_error
from app.audit.domain.event import Stage


class Retryability(StrEnum):
    """失敗が同一入力の将来再実行で回復しうるか。"""

    RETRYABLE = "retryable"
    NON_RETRYABLE = "non_retryable"
    UNKNOWN = "unknown"


class FailureAction(StrEnum):
    """監査対象として明示する業務副作用。"""

    DROP_ARTICLE = "drop_article"


@dataclass(frozen=True, slots=True)
class FailureProjection:
    """DB wire 値へ落とす前の失敗属性。"""

    failure_kind: str
    retryability: Retryability
    failure_action: FailureAction | None
    code: str
    stage: Stage | None = None


class FailurePayloadFields(TypedDict):
    """stage payload へ展開する失敗属性 keyword。

    ``dict[str, str | None]`` だと ``**`` 展開時に ``kind`` など任意 keyword へ
    流入し得ると型検査されるため、key set を固定する。
    """

    failure_kind: str
    failure_action: str | None


def failure_payload_fields(
    projection: FailureProjection,
) -> FailurePayloadFields:
    """失敗 projection から stage payload 用の属性 dict を作る。"""
    return {
        "failure_kind": projection.failure_kind,
        "failure_action": failure_action_value(projection),
    }


def failure_action_value(projection: FailureProjection) -> str | None:
    """payload に保存する ``failure_action`` の wire 値を返す。"""
    return (
        projection.failure_action.value
        if projection.failure_action is not None
        else None
    )


def project_failure(
    exc: BaseException, *, fallback_code: str = "unexpected_error"
) -> FailureProjection:
    """自前 marker / DB 例外 / catch-all を失敗属性へ投影する。"""
    marker = project_marker_failure(exc)
    if marker is not None:
        return marker

    db = project_db_failure(exc)
    if db is not None:
        return db

    return unknown_failure_projection(code=fallback_code)


def project_marker_failure(exc: BaseException) -> FailureProjection | None:
    """ClassVar を持つ自前 marker 例外を失敗属性へ投影する。"""
    stage = getattr(exc, "STAGE", None)
    failure_kind = getattr(exc, "FAILURE_KIND", None)
    retryability = getattr(exc, "RETRYABILITY", None)
    if not isinstance(stage, Stage):
        return None
    if not isinstance(failure_kind, str):
        return None
    if not isinstance(retryability, Retryability):
        return None

    failure_action = getattr(exc, "FAILURE_ACTION", None)
    if failure_action is not None and not isinstance(failure_action, FailureAction):
        return None

    code = _code_of_marker(exc)
    if code is None:
        return None

    return FailureProjection(
        failure_kind=failure_kind,
        retryability=retryability,
        failure_action=failure_action,
        code=code,
        stage=stage,
    )


def project_db_failure(exc: BaseException) -> FailureProjection | None:
    """SQLAlchemy 例外を失敗属性へ投影する。"""
    db = classify_db_error(exc)
    if db is None:
        return None

    if db.cause is DbErrorCause.RUNTIME:
        return FailureProjection(
            failure_kind="db_runtime",
            retryability=Retryability.RETRYABLE,
            failure_action=None,
            code=db.code,
        )
    if db.cause is DbErrorCause.CONSTRAINT:
        return FailureProjection(
            failure_kind="db_constraint",
            retryability=Retryability.NON_RETRYABLE,
            failure_action=None,
            code=db.code,
        )
    if db.cause is DbErrorCause.QUERY_OR_SCHEMA:
        return FailureProjection(
            failure_kind="db_query_or_schema",
            retryability=Retryability.NON_RETRYABLE,
            failure_action=None,
            code=db.code,
        )
    return FailureProjection(
        failure_kind="db_unknown",
        retryability=Retryability.UNKNOWN,
        failure_action=None,
        code=db.code,
    )


def unknown_failure_projection(*, code: str = "unexpected_error") -> FailureProjection:
    """分類不能な例外用の catch-all projection。"""
    return FailureProjection(
        failure_kind="unknown",
        retryability=Retryability.UNKNOWN,
        failure_action=None,
        code=code,
    )


def _code_of_marker(exc: BaseException) -> str | None:
    code = getattr(exc, "code", None)
    if isinstance(code, str) and code:
        return code

    class_code = getattr(exc, "CODE", None)
    if isinstance(class_code, str) and class_code:
        return class_code
    return None
