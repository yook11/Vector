"""Direct Answer工程の分類済み失敗。"""

from __future__ import annotations

from app.agent.answering.direct_answer.contract import DirectAnswerInvalidError
from app.agent.error_type import span_error_type
from app.ai_providers.errors import AIProviderError
from app.shared.errors import ApplicationError

__all__ = ["DirectAnswerError"]

_TIMEOUT_CODE = "direct_answer_timeout"


class DirectAnswerError(ApplicationError):
    """直接回答の工程が、分類済みの原因で回答を作れなかった。"""

    def __init__(
        self, *, cause: AIProviderError | DirectAnswerInvalidError | TimeoutError
    ) -> None:
        self.cause = cause
        self.__cause__ = cause
        super().__init__("直接回答を作れませんでした", details={"code": self.code})

    @property
    def code(self) -> str:
        """観測に使う失敗の種別で、原因の種別をそのまま名乗る。"""
        if isinstance(self.cause, TimeoutError):
            return _TIMEOUT_CODE
        return span_error_type(self.cause)
