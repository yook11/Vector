"""Assessment工程で確定した失敗理由と応答不正の詳細。"""

from __future__ import annotations

from enum import StrEnum

from app.shared.errors import ApplicationError


class AssessmentFailureReason(StrEnum):
    """呼び出し側の再試行方針とは独立した失敗理由。"""

    RESPONSE_INVALID = "response_invalid"
    CURATION_MISSING = "curation_missing"


class AssessmentError(ApplicationError):
    """失敗理由と原因の詳細を呼び出し元へ伝える。"""

    def __init__(
        self,
        *,
        reason: AssessmentFailureReason,
        message: str | None = None,
        defect: StrEnum | None = None,
    ) -> None:
        if not isinstance(reason, AssessmentFailureReason):
            raise TypeError("reason must be an AssessmentFailureReason")
        if reason is AssessmentFailureReason.RESPONSE_INVALID:
            if not isinstance(defect, StrEnum):
                raise TypeError("defect must be a StrEnum member")
        elif defect is not None:
            raise TypeError("defect requires RESPONSE_INVALID reason")
        self.reason = reason
        self.defect = defect
        if message is not None and not isinstance(message, str):
            raise TypeError("message must be a string or None")
        default_message = {
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
