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
    failure_code: str | None = None,
) -> None:
    """Record one final answer synthesis outcome with low-cardinality labels.

    failure_code には classifier の code のみを渡す (自由文禁止)。None は成功。
    """

    _answer_synthesis_outcome_counter.add(
        1,
        attributes={
            "result": result,
            "retry_used": retry_used,
            "status": status,
            "fallback_used": fallback_used,
            "failure_code": failure_code if failure_code is not None else "none",
        },
    )


def record_direct_answer_outcome(
    *,
    result: DirectAnswerOutcomeResult,
    retry_used: bool,
    failure_code: str | None = None,
) -> None:
    """Record one final direct answer outcome with low-cardinality labels.

    failure_code には classifier の code のみを渡す (自由文禁止)。None は成功。
    """

    _direct_answer_outcome_counter.add(
        1,
        attributes={
            "result": result,
            "retry_used": retry_used,
            "failure_code": failure_code if failure_code is not None else "none",
        },
    )
