"""Consumerの失敗を監査情報・枯渇通知対象・受信完了の対象へ投影する純粋関数。"""

from __future__ import annotations

from dataclasses import dataclass

from app.analysis.ai_provider_exhaustion import (
    ExhaustedProviderError,
    exhausted_provider_error,
)
from app.analysis.ai_provider_settlement import (
    SettledProviderFailure,
    settled_provider_failure,
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
    """失敗後処理と、再配信せずに受信完了にするかの判断に必要な情報。"""

    audit: FailureProjection
    provider_exhaustion: ExhaustedProviderError | None = None
    settled: SettledProviderFailure | None = None


def classify_curation_failure(exc: Exception) -> CurationFailureClassification:
    """Serviceの失敗理由を監査情報・枯渇通知対象・受信完了の対象へ対応付ける。"""
    if isinstance(exc, CurationError):
        if exc.reason is CurationFailureReason.PROVIDER_ERROR:
            provider_error = exc.provider_error
            if provider_error is None:
                raise TypeError("provider error is required")
            return CurationFailureClassification(
                audit=FailureProjection(
                    code=exc.code,
                    failure_kind=None,
                    failure_reason=provider_error.reason.value,
                    retryability=None,
                    failure_action=None,
                ),
                provider_exhaustion=exhausted_provider_error(provider_error),
                settled=settled_provider_failure(provider_error),
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
