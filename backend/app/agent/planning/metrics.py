"""Question planner metrics."""

from __future__ import annotations

from typing import Literal

import logfire

from app.agent.contract import PlanType

PlannerOutcomeResult = Literal["planned", "failed"]

_planner_outcome_counter = logfire.metric_counter(
    "vector.agent.planner.outcome",
    unit="1",
    description="Question planner final outcome per request",
)


def record_question_planner_outcome(
    *,
    result: PlannerOutcomeResult,
    retry_used: bool,
    plan_type: PlanType | Literal["not_created"],
    failure_code: str | None = None,
) -> None:
    """Record one final planner outcome with low-cardinality labels.

    failure_code には classifier の code のみを渡す (自由文禁止)。None は成功。
    """

    _planner_outcome_counter.add(
        1,
        attributes={
            "result": result,
            "retry_used": retry_used,
            "plan_type": plan_type,
            "failure_code": failure_code if failure_code is not None else "none",
        },
    )
