"""Evidence Review attemptの分類済み失敗。"""

from __future__ import annotations

from pydantic import ValidationError

from app.agent.runtime.contract import AgentResponseDefect, AgentResponseInvalidError
from app.analysis.ai_provider_errors import (
    AIProviderContentError,
    AIProviderStateError,
)

__all__ = ["EvidenceReviewError", "evidence_review_error_from"]

_REVIEWER_TIMEOUT = "reviewer_timeout"

type _EvidenceReviewSourceError = (
    AgentResponseInvalidError
    | AIProviderStateError
    | AIProviderContentError
    | TimeoutError
    | ValidationError
)


class EvidenceReviewError(Exception):
    """既知の理由によりEvidence Review attemptを完了できなかった。"""

    def __init__(self, *, code: str) -> None:
        if not code.strip():
            raise ValueError("code must not be blank")
        self.code = code
        super().__init__(code)


def evidence_review_error_from(
    cause: _EvidenceReviewSourceError,
) -> EvidenceReviewError:
    """attempt由来の失敗を自由文を含まない工程codeへ写す。"""

    if isinstance(cause, AgentResponseInvalidError):
        return EvidenceReviewError(code=cause.defect.value)
    if isinstance(cause, AIProviderStateError | AIProviderContentError):
        code = cause.reason.value if cause.reason is not None else cause.CODE
        return EvidenceReviewError(code=code)
    if isinstance(cause, TimeoutError):
        return EvidenceReviewError(code=_REVIEWER_TIMEOUT)
    if isinstance(cause, ValidationError):
        return EvidenceReviewError(
            code=AgentResponseDefect.OUTPUT_SCHEMA_MISMATCH.value
        )
    raise TypeError(f"unsupported evidence review error: {type(cause).__name__}")
