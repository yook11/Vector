"""Question planner metrics."""

from __future__ import annotations

from typing import Literal

import logfire

from app.agent.contract import RetrievalMode

PlannerOutcomeResult = Literal["planned", "fallback", "failed"]

_planner_outcome_counter = logfire.metric_counter(
    "vector.agent.planner.outcome",
    unit="1",
    description="Question planner final outcome per request",
)


def record_question_planner_outcome(
    *,
    result: PlannerOutcomeResult,
    retry_used: bool,
    planned_retrieval_mode: RetrievalMode | Literal["unknown"],
) -> None:
    """Record one final planner outcome with low-cardinality labels."""

    _planner_outcome_counter.add(
        1,
        attributes={
            "result": result,
            "retry_used": retry_used,
            "planned_retrieval_mode": planned_retrieval_mode,
        },
    )
