"""Evidence review metrics."""

from __future__ import annotations

from typing import Literal

import logfire

EvidenceReviewOutcome = Literal["completed", "failed"]

_evidence_review_outcome_counter = logfire.metric_counter(
    "vector.agent.evidence_review.outcome",
    unit="1",
    description="Evidence review final outcome per request",
)


def record_evidence_review_outcome(
    *,
    result: EvidenceReviewOutcome,
    retry_used: bool,
) -> None:
    """Record one final evidence review outcome with low-cardinality labels."""

    _evidence_review_outcome_counter.add(
        1,
        attributes={
            "result": result,
            "retry_used": retry_used,
        },
    )
