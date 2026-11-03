"""Evidence Review attemptの分類済み失敗。"""

from __future__ import annotations

from pydantic import ValidationError

from app.agent.runtime.contract import AgentResponseDefect, AgentResponseInvalidError
from app.ai_providers.errors import AIProviderError
from app.shared.errors import ApplicationError

__all__ = ["EvidenceReviewError"]

_REVIEWER_TIMEOUT = "reviewer_timeout"


class EvidenceReviewError(ApplicationError):
    """精査の工程が、分類済みの原因で根拠を選べなかった。"""

    def __init__(
        self,
        *,
        cause: AgentResponseInvalidError
        | AIProviderError
        | TimeoutError
        | ValidationError,
    ) -> None:
        self.cause = cause
        self.__cause__ = cause
        super().__init__("根拠を精査できませんでした", details={"code": self.code})

    @property
    def code(self) -> str:
        """観測に使う失敗の種別で、原因の自由文を含めない。"""
        if isinstance(self.cause, AgentResponseInvalidError):
            return self.cause.defect.value
        if isinstance(self.cause, AIProviderError):
            return self.cause.reason.value
        if isinstance(self.cause, TimeoutError):
            return _REVIEWER_TIMEOUT
        return AgentResponseDefect.OUTPUT_SCHEMA_MISMATCH.value
