"""Question context metrics."""

from __future__ import annotations

from typing import Literal

import logfire

QuestionContextOutcome = Literal["resolved", "skipped", "failed"]

_question_context_outcome_counter = logfire.metric_counter(
    "vector.agent.question_resolution.outcome",
    unit="1",
    description="Question context outcome per agent run",
)


def record_question_context_outcome(*, result: QuestionContextOutcome) -> None:
    """Record the final context outcome without conversation content."""

    _question_context_outcome_counter.add(1, attributes={"result": result})
