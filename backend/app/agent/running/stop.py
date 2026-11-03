"""Run attemptが止まったことを回答の各層へ伝える例外。"""

from __future__ import annotations

from app.agent.running.state import RunFinished, RunRejected

__all__ = ["AnswerGenerationStopped"]


class AnswerGenerationStopped(Exception):
    """現在のrun attemptが回答生成を継続できなくなった。"""

    def __init__(self, outcome: RunFinished | RunRejected) -> None:
        super().__init__(
            outcome.reason.value
            if isinstance(outcome, RunRejected)
            else outcome.status.value
        )
        self.outcome = outcome
