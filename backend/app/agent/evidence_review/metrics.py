"""Evidence review metrics."""

from __future__ import annotations

from typing import Literal

import logfire

EvidenceReviewOutcomeResult = Literal["succeeded", "failed"]

_evidence_review_outcome_counter = logfire.metric_counter(
    "vector.agent.evidence_review.outcome",
    unit="1",
    description="Evidence review final outcome per request",
)


def record_evidence_review_outcome(
    *,
    result: EvidenceReviewOutcomeResult,
    attempt_count: int,
    failure_code: str | None = None,
) -> None:
    """Record one final evidence review outcome with low-cardinality labels.

    failure_codeには工程で分類したcodeだけを渡す。Noneは成功を表す。
    """

    _evidence_review_outcome_counter.add(
        1,
        attributes={
            "result": result,
            "attempt_count": attempt_count,
            "failure_code": failure_code if failure_code is not None else "none",
        },
    )
