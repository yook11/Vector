"""SDK-free planner error types."""

from __future__ import annotations

from enum import StrEnum

__all__ = ["QuestionPlannerResponseInvalidError"]


class QuestionPlannerResponseInvalidError(ValueError):
    """Planner response が ``QuestionPlanDraft`` として消化できない。"""

    def __init__(self, defect: StrEnum) -> None:
        self.defect = defect
        super().__init__(defect.value)
