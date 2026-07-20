"""Input safety outcome metrics."""

from __future__ import annotations

from typing import Literal

import logfire

from app.agent.input_safety.contract import InputSafetyBlockReason

InputSafetyOutcome = Literal["allow", "block", "failed"]

_input_safety_outcome_counter = logfire.metric_counter(
    "vector.agent.input_safety.outcome",
    unit="1",
    description="Input safety outcome per agent run",
)


def record_input_safety_outcome(
    *,
    result: InputSafetyOutcome,
    block_reason: InputSafetyBlockReason | None,
) -> None:
    _input_safety_outcome_counter.add(
        1,
        attributes={
            "result": result,
            "block_reason": block_reason.value if block_reason is not None else "none",
        },
    )
