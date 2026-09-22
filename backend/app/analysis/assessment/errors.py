"""Assessmentの失敗理由とプロバイダー・応答不正の詳細。"""

from __future__ import annotations

from enum import StrEnum

from app.ai_providers.errors import (
    CLASSIFIED_AI_PROVIDER_ERRORS,
    AIProviderError,
)
from app.shared.errors import ApplicationError


class AssessmentFailureReason(StrEnum):
    """呼び出し側の再試行方針とは独立した失敗理由。"""

    PROVIDER_ERROR = "provider_error"
    RESPONSE_INVALID = "response_invalid"
    CURATION_MISSING = "curation_missing"


class AssessmentError(ApplicationError):
    """失敗理由と原因の詳細を呼び出し元へ伝える。"""

    def __init__(
        self,
        *,
        reason: AssessmentFailureReason,
        message: str | None = None,
        provider_error: AIProviderError | None = None,
        defect: StrEnum | None = None,
    ) -> None:
        if not isinstance(reason, AssessmentFailureReason):
            raise TypeError("reason must be an AssessmentFailureReason")
        if reason is AssessmentFailureReason.PROVIDER_ERROR:
            if not isinstance(provider_error, CLASSIFIED_AI_PROVIDER_ERRORS):
                raise TypeError("provider_error must be a classified AI provider error")
        elif provider_error is not None:
            raise TypeError("provider_error requires PROVIDER_ERROR reason")
        if reason is AssessmentFailureReason.RESPONSE_INVALID:
            if not isinstance(defect, StrEnum):
                raise TypeError("defect must be a StrEnum member")
        elif defect is not None:
            raise TypeError("defect requires RESPONSE_INVALID reason")
        self.reason = reason
        self.provider_error = provider_error
        self.defect = defect
        if message is not None and not isinstance(message, str):
            raise TypeError("message must be a string or None")
        default_message = {
            AssessmentFailureReason.PROVIDER_ERROR: (
                "AIプロバイダーの処理失敗により記事を判定できませんでした"
            ),
            AssessmentFailureReason.RESPONSE_INVALID: (
                "AI応答が記事判定の契約を満たしていません"
            ),
            AssessmentFailureReason.CURATION_MISSING: (
                "判定対象のCurationが存在しません"
            ),
        }[reason]
        super().__init__(
            default_message if message is None else message,
            details={"reason": reason.value, "code": self.code},
        )

    @property
    def code(self) -> str:
        """観測コードには原因の種別ラベルだけを用いる。"""
        if self.provider_error is not None:
            return self.provider_error.CODE
        if self.defect is not None:
            return self.defect.value
        return "assessment_curation_missing"


class AssessmentResponseInvalidError(AssessmentError):
    """応答不正の詳細は検知場所が所有する列挙型で受け取る。"""

    def __init__(self, defect: StrEnum, *, message: str | None = None) -> None:
        super().__init__(
            reason=AssessmentFailureReason.RESPONSE_INVALID,
            defect=defect,
            message=message,
        )


class AssessmentCurationMissingError(AssessmentError):
    """処理対象のCurationが存在しない。"""

    def __init__(self) -> None:
        super().__init__(reason=AssessmentFailureReason.CURATION_MISSING)


def to_assessment_error(exc: AIProviderError) -> AssessmentError:
    """プロバイダー例外を保持し、Serviceの失敗理由を付与する。"""
    if not isinstance(exc, CLASSIFIED_AI_PROVIDER_ERRORS):
        raise TypeError(f"unmapped provider error: {type(exc).__qualname__}")
    return AssessmentError(
        reason=AssessmentFailureReason.PROVIDER_ERROR, provider_error=exc
    )
