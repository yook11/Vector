"""Question planner の分類済み失敗。"""

from __future__ import annotations

from app.agent.runtime.contract import AgentResponseInvalidError
from app.analysis.ai_provider_errors import (
    AIProviderContentError,
    AIProviderStateError,
)

__all__ = ["PlanningError", "planning_error_from"]


class PlanningError(Exception):
    """既知の理由により QuestionPlan を作成できなかった。"""

    def __init__(self, *, code: str) -> None:
        if not code:
            raise ValueError("code must not be empty")
        self.code = code
        super().__init__(code)


def planning_error_from(
    cause: AIProviderStateError | AIProviderContentError | AgentResponseInvalidError,
) -> PlanningError:
    if isinstance(cause, AgentResponseInvalidError):
        return PlanningError(code=cause.defect.value)
    return PlanningError(code=cause.CODE)
