"""Question context metrics."""

from __future__ import annotations

from typing import Literal

import logfire

QuestionContextOutcome = Literal["prepared", "failed"]

_question_context_outcome_counter = logfire.metric_counter(
    "vector.agent.question_context.outcome",
    unit="1",
    description="Question context outcome per agent run",
)


def record_question_context_outcome(
    *,
    result: QuestionContextOutcome,
    explicit_feedback_detected: bool,
    previous_answer_had_missing_aspects: bool,
) -> None:
    """Record the final context outcome without conversation content."""

    _question_context_outcome_counter.add(
        1,
        attributes={
            "result": result,
            "explicit_feedback_detected": explicit_feedback_detected,
            "previous_answer_had_missing_aspects": previous_answer_had_missing_aspects,
        },
    )
