"""run attempt を止める理由と、止まったことを回答の各層へ伝える例外。"""

from __future__ import annotations

from enum import StrEnum

__all__ = ["AnswerGenerationStopped", "StopReason"]


class StopReason(StrEnum):
    DEADLINE_EXCEEDED = "deadline_exceeded"
    NOT_CURRENT = "not_current"


class AnswerGenerationStopped(Exception):
    """現在のrun attemptが回答生成を継続できなくなった。"""

    def __init__(
        self,
        reason: StopReason = StopReason.NOT_CURRENT,
    ) -> None:
        super().__init__(reason.value)
        self.reason = reason
