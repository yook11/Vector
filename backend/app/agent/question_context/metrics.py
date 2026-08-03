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
    prompt_version: str,
    ai_model: str,
    failure_code: str | None = None,
) -> None:
    """Record the final context outcome without conversation content."""

    attributes: dict[str, str] = {
        "result": result,
        "prompt_version": prompt_version,
        "ai_model": ai_model,
    }
    if failure_code is not None:
        attributes["failure_code"] = failure_code
    _question_context_outcome_counter.add(
        1,
        attributes=attributes,
    )
