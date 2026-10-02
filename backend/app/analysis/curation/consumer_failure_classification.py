"""Consumerの失敗を監査の失敗属性へ投影する純粋関数。"""

from __future__ import annotations

from app.ai_providers.errors import AIProviderError
from app.analysis.curation.errors import CurationError, CurationFailureReason
from app.audit.failure_projection import (
    FailureProjection,
    Retryability,
    project_db_failure,
    unknown_failure_projection,
)


def classify_curation_failure(exc: Exception) -> FailureProjection:
    """AIの失敗・Serviceの失敗理由・DB障害を監査の失敗属性へ対応付ける。"""
    if isinstance(exc, AIProviderError):
        return FailureProjection(
            code=exc.CODE,
            failure_kind=None,
            failure_reason=exc.reason.value,
            retryability=None,
            failure_action=None,
        )
    if isinstance(exc, CurationError):
        if exc.reason is CurationFailureReason.RESPONSE_INVALID:
            return FailureProjection(
                code=exc.code,
                failure_kind="ai_response_invalid",
                retryability=Retryability.RETRYABLE,
                failure_action=None,
            )
        raise TypeError("unmapped curation failure reason")

    db_failure = project_db_failure(exc)
    if db_failure is not None:
        return db_failure
    return unknown_failure_projection()
