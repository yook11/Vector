"""Consumerの失敗から、再配信に任せるか受信完了にするかを決める純粋関数。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import assert_never

from app.ai_providers.errors import AIProviderError
from app.analysis.ai_provider_retry import is_unrecoverable_for_input
from app.analysis.assessment.domain.ready import AssessmentReadyBuildRejected
from app.analysis.assessment.errors import AssessmentError, AssessmentFailureReason
from app.db.errors import DatabaseError
from app.db.retry_prospect import is_resolvable_by_retry


@dataclass(frozen=True, slots=True)
class RetryAssessment:
    """SQS の再配信に任せる Assessment の失敗。"""

    error: Exception


@dataclass(frozen=True, slots=True)
class NoRetryAssessment:
    """再配信しても変わらないため受信完了にする Assessment の失敗。"""

    cause: (
        AssessmentReadyBuildRejected | AIProviderError | AssessmentError | DatabaseError
    )


def classify_assessment_failure(
    exc: Exception,
) -> RetryAssessment | NoRetryAssessment:
    """同じ入力では変わらない失敗と解消しないDBの失敗を受信完了にし、想定外は再配信する。"""
    if isinstance(exc, AIProviderError):
        if is_unrecoverable_for_input(exc):
            return NoRetryAssessment(exc)
        return RetryAssessment(exc)
    if isinstance(exc, AssessmentError):
        match exc.reason:
            case AssessmentFailureReason.CURATION_MISSING:
                return NoRetryAssessment(exc)
            case AssessmentFailureReason.RESPONSE_INVALID:
                return RetryAssessment(exc)
            case _:
                assert_never(exc.reason)
    if isinstance(exc, DatabaseError):
        if is_resolvable_by_retry(exc):
            return RetryAssessment(exc)
        return NoRetryAssessment(exc)
    return RetryAssessment(exc)
