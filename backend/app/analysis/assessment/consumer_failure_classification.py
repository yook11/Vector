"""Consumerの失敗を監査情報と枯渇通知対象へ投影する純粋関数。"""

from __future__ import annotations

from dataclasses import dataclass

from app.analysis.ai_provider_exhaustion import (
    ExhaustedProviderError,
    exhausted_provider_error,
)
from app.analysis.assessment.errors import AssessmentError, AssessmentFailureReason
from app.audit.failure_projection import (
    FailureProjection,
    Retryability,
    project_db_failure,
    unknown_failure_projection,
)


@dataclass(frozen=True, slots=True)
class AssessmentFailureClassification:
    """再配信の判断を含まない、失敗後処理に必要な情報。"""

    audit: FailureProjection
    provider_exhaustion: ExhaustedProviderError | None = None


def classify_assessment_failure(exc: Exception) -> AssessmentFailureClassification:
    """Serviceの失敗理由を監査情報と枯渇通知対象へ対応付ける。"""
    if isinstance(exc, AssessmentError):
        if exc.reason is AssessmentFailureReason.PROVIDER_ERROR:
            provider_error = exc.provider_error
            if provider_error is None:
                raise TypeError("provider error is required")
            mode = provider_error.FAILURE_MODE
            return AssessmentFailureClassification(
                audit=FailureProjection(
                    code=exc.code,
                    failure_kind=mode.value,
                    failure_reason=provider_error.reason.value
                    if provider_error.reason is not None
                    else None,
                    retryability=Retryability.RETRYABLE
                    if mode.retryable
                    else Retryability.NON_RETRYABLE,
                    failure_action=None,
                ),
                provider_exhaustion=exhausted_provider_error(provider_error),
            )
        if exc.reason is AssessmentFailureReason.CURATION_MISSING:
            return AssessmentFailureClassification(
                audit=FailureProjection(
                    code=exc.code,
                    failure_kind="target_missing",
                    retryability=Retryability.NON_RETRYABLE,
                    failure_action=None,
                ),
            )
        if exc.reason is AssessmentFailureReason.RESPONSE_INVALID:
            return AssessmentFailureClassification(
                audit=FailureProjection(
                    code=exc.code,
                    failure_kind="ai_response_invalid",
                    retryability=Retryability.RETRYABLE,
                    failure_action=None,
                ),
            )
        raise TypeError("unmapped assessment failure reason")

    db_failure = project_db_failure(exc)
    if db_failure is not None:
        return AssessmentFailureClassification(audit=db_failure)
    return AssessmentFailureClassification(audit=unknown_failure_projection())
