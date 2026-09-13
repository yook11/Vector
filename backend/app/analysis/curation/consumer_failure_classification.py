"""Consumerの失敗を監査情報と枯渇通知対象へ投影する純粋関数。"""

from __future__ import annotations

from dataclasses import dataclass

from app.analysis.ai_provider_exhaustion import (
    ExhaustedProviderError,
    exhausted_provider_error,
)
from app.analysis.curation.errors import CurationError, CurationFailureReason
from app.audit.failure_projection import (
    FailureProjection,
    Retryability,
    project_db_failure,
    unknown_failure_projection,
)


@dataclass(frozen=True, slots=True)
class CurationFailureClassification:
    """再配信の判断を含まない、失敗後処理に必要な情報。"""

    audit: FailureProjection
    provider_exhaustion: ExhaustedProviderError | None = None


def classify_curation_failure(exc: Exception) -> CurationFailureClassification:
    """Serviceの失敗理由を監査情報と枯渇通知対象へ対応付ける。"""
    if isinstance(exc, CurationError):
        if exc.reason is CurationFailureReason.PROVIDER_ERROR:
            provider_error = exc.provider_error
            if provider_error is None:
                raise TypeError("provider error is required")
            mode = provider_error.FAILURE_MODE
            return CurationFailureClassification(
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
        if exc.reason is CurationFailureReason.RESPONSE_INVALID:
            return CurationFailureClassification(
                audit=FailureProjection(
                    code=exc.code,
                    failure_kind="ai_response_invalid",
                    retryability=Retryability.RETRYABLE,
                    failure_action=None,
                ),
            )
        raise TypeError("unmapped curation failure reason")

    db_failure = project_db_failure(exc)
    if db_failure is not None:
        return CurationFailureClassification(audit=db_failure)
    return CurationFailureClassification(audit=unknown_failure_projection())
