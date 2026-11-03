"""申し送りの整理工程の分類済み失敗。"""

from __future__ import annotations

from app.agent.error_type import span_error_type
from app.agent.runtime.contract import AgentResponseInvalidError
from app.ai_providers.errors import AIProviderError
from app.shared.errors import ApplicationError

__all__ = ["ResearchHandoffError"]


class ResearchHandoffError(ApplicationError):
    """申し送りの整理の工程が、分類済みの原因で整理を書き直せなかった。"""

    def __init__(self, *, cause: AIProviderError | AgentResponseInvalidError) -> None:
        self.cause = cause
        self.__cause__ = cause
        super().__init__("申し送りを整理できませんでした", details={"code": self.code})

    @property
    def code(self) -> str:
        """観測に使う失敗の種別で、原因の種別をそのまま名乗る。"""
        return span_error_type(self.cause)
