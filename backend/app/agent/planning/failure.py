"""Question planner の分類済み失敗。"""

from __future__ import annotations

from app.agent.error_type import span_error_type
from app.agent.output import AgentOutputInvalidError
from app.ai_providers.errors import AIProviderError
from app.shared.errors import ApplicationError

__all__ = ["PlanningError"]

_TIMEOUT_CODE = "planning_timeout"


class PlanningError(ApplicationError):
    """計画の工程が、分類済みの原因で質問の計画を作れなかった。"""

    def __init__(
        self, *, cause: AIProviderError | AgentOutputInvalidError | TimeoutError
    ) -> None:
        self.cause = cause
        self.__cause__ = cause
        super().__init__("質問の計画を作れませんでした", details={"code": self.code})

    @property
    def code(self) -> str:
        """観測に使う失敗の種別で、原因の種別をそのまま名乗る。"""
        if isinstance(self.cause, TimeoutError):
            return _TIMEOUT_CODE
        return span_error_type(self.cause)
