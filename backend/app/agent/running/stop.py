"""Run attemptが止まったことを回答の各層へ伝える例外。"""

from __future__ import annotations

from app.agent.running.state import CannotContinue

__all__ = ["AnswerGenerationStopped"]


class AnswerGenerationStopped(Exception):
    """現在のrun attemptが回答生成を継続できなくなった。"""

    def __init__(self, stopped: CannotContinue) -> None:
        super().__init__(type(stopped).__name__)
        self.stopped = stopped
