"""Question planner public contract."""

from __future__ import annotations

from typing import Protocol

from app.agent.contract import AnswerQuestionInput
from app.agent.planning.contract import QuestionPlan

__all__ = ["QuestionPlanner"]


class QuestionPlanner(Protocol):
    """Planner boundary that returns a completed ``QuestionPlan``."""

    async def plan(self, input: AnswerQuestionInput) -> QuestionPlan: ...
