"""Question-resolution metrics."""

from __future__ import annotations

from typing import Literal

import logfire

QuestionResolutionOutcome = Literal["resolved", "skipped", "failed"]

_question_resolution_outcome_counter = logfire.metric_counter(
    "vector.agent.question_resolution.outcome",
    unit="1",
    description="Question resolution outcome per agent run",
)


def record_question_resolution_outcome(*, result: QuestionResolutionOutcome) -> None:
    """Record the final resolution outcome without conversation content."""

    _question_resolution_outcome_counter.add(1, attributes={"result": result})
