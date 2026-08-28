"""Answer generation metrics."""

from __future__ import annotations

from typing import Literal

import logfire

EvidenceAnswerOutcomeResult = Literal["succeeded", "failed"]
DirectAnswerOutcomeResult = Literal["succeeded", "failed"]

_evidence_answer_outcome_counter = logfire.metric_counter(
    "vector.agent.evidence_answer.outcome",
    unit="1",
    description="Evidence answer final outcome per request",
)
_direct_answer_outcome_counter = logfire.metric_counter(
    "vector.agent.direct_answer.outcome",
    unit="1",
    description="Direct answer final outcome per request",
)


def record_evidence_answer_outcome(
    *,
    result: EvidenceAnswerOutcomeResult,
    attempt_count: int,
    failure_code: str | None = None,
) -> None:
    """Record one final evidence answer outcome with low-cardinality labels.

    failure_code には classifier の code のみを渡す (自由文禁止)。None は成功。
    """

    _evidence_answer_outcome_counter.add(
        1,
        attributes={
            "result": result,
            "attempt_count": attempt_count,
            "failure_code": failure_code if failure_code is not None else "none",
        },
    )


def record_direct_answer_outcome(
    *,
    result: DirectAnswerOutcomeResult,
    attempt_count: int,
    failure_code: str | None = None,
) -> None:
    """Record one final direct answer outcome with low-cardinality labels.

    failure_code には classifier の code のみを渡す (自由文禁止)。None は成功。
    """

    _direct_answer_outcome_counter.add(
        1,
        attributes={
            "result": result,
            "attempt_count": attempt_count,
            "failure_code": failure_code if failure_code is not None else "none",
        },
    )
