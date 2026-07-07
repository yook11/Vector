"""Answer synthesis metrics."""

from __future__ import annotations

from typing import Literal

import logfire

AnswerSynthesisOutcomeResult = Literal["synthesized", "fallback", "failed"]
AnswerSynthesisStatus = Literal["answered", "insufficient", "unknown"]
DirectAnswerOutcomeResult = Literal["answered", "failed"]

_answer_synthesis_outcome_counter = logfire.metric_counter(
    "vector.agent.answer_synthesis.outcome",
    unit="1",
    description="Evidence answer synthesis final outcome per request",
)
_direct_answer_outcome_counter = logfire.metric_counter(
    "vector.agent.direct_answer.outcome",
    unit="1",
    description="Direct answer final outcome per request",
)


def record_answer_synthesis_outcome(
    *,
    result: AnswerSynthesisOutcomeResult,
    retry_used: bool,
    status: AnswerSynthesisStatus,
    fallback_used: bool,
) -> None:
    """Record one final answer synthesis outcome with low-cardinality labels."""

    _answer_synthesis_outcome_counter.add(
        1,
        attributes={
            "result": result,
            "retry_used": retry_used,
            "status": status,
            "fallback_used": fallback_used,
        },
    )


def record_direct_answer_outcome(
    *,
    result: DirectAnswerOutcomeResult,
    retry_used: bool,
) -> None:
    """Record one final direct answer outcome with low-cardinality labels."""

    _direct_answer_outcome_counter.add(
        1,
        attributes={
            "result": result,
            "retry_used": retry_used,
        },
    )
