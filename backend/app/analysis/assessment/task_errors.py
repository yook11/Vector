"""既存Taskiq向けの失敗分類とServiceエラーの変換。"""

from __future__ import annotations

from typing import ClassVar

from app.ai_providers.errors import AIProviderError
from app.analysis.assessment.errors import AssessmentError, AssessmentFailureReason
from app.audit.failure_projection import FailureAction, Retryability
from app.logfire.exceptions import VectorDomainError


class AssessmentTaskError(VectorDomainError):
    """既存Taskiqの監査・再試行に用いる失敗分類。"""


class AssessmentRecoverableError(AssessmentTaskError):
    """既存Taskiqで再試行の対象となるAssessment失敗。"""

    SAFE_ATTRS: ClassVar[tuple[str, ...]] = ("code",)
    RETRYABILITY: ClassVar[Retryability] = Retryability.RETRYABLE
    FAILURE_ACTION: ClassVar[FailureAction | None] = None

    code: str
    failure_kind: str
    failure_reason: str | None
    provider_error: AIProviderError | None

    def __init__(
        self,
        *,
        code: str,
        failure_kind: str,
        failure_reason: str | None = None,
        provider_error: AIProviderError | None = None,
    ) -> None:
        super().__init__()
        self.code = code
        self.failure_kind = failure_kind
        self.failure_reason = failure_reason
        self.provider_error = provider_error


class AssessmentTerminalError(AssessmentTaskError):
    """既存Taskiqで再試行しないAssessment失敗。"""

    SAFE_ATTRS: ClassVar[tuple[str, ...]] = ("code",)
    RETRYABILITY: ClassVar[Retryability] = Retryability.NON_RETRYABLE
    FAILURE_ACTION: ClassVar[FailureAction | None] = None

    code: str
    failure_kind: str
    failure_reason: str | None
    provider_error: AIProviderError | None

    def __init__(
        self,
        *,
        code: str,
        failure_kind: str,
        failure_reason: str | None = None,
        provider_error: AIProviderError | None = None,
    ) -> None:
        super().__init__()
        self.code = code
        self.failure_kind = failure_kind
        self.failure_reason = failure_reason
        self.provider_error = provider_error


def to_assessment_task_error(exc: BaseException) -> BaseException:
    """業務上の失敗理由を既存Taskiqの再試行分類へ対応付ける。"""
    if not isinstance(exc, AssessmentError):
        return exc
    if exc.reason is AssessmentFailureReason.PROVIDER_ERROR:
        provider = exc.provider_error
        if provider is None:
            raise TypeError("provider error is required")
        mode = provider.FAILURE_MODE
        marker = (
            AssessmentRecoverableError if mode.retryable else AssessmentTerminalError
        )
        result = marker(
            code=exc.code,
            failure_kind=mode.value,
            failure_reason=provider.reason.value
            if provider.reason is not None
            else None,
            provider_error=provider,
        )
    elif exc.reason is AssessmentFailureReason.RESPONSE_INVALID:
        result = AssessmentRecoverableError(
            code=exc.code, failure_kind="ai_response_invalid"
        )
    elif exc.reason is AssessmentFailureReason.CURATION_MISSING:
        result = AssessmentTerminalError(code=exc.code, failure_kind="target_missing")
    else:
        raise TypeError("unmapped assessment failure reason")
    result.__cause__ = exc
    return result
