"""精査工程 review() 1回の start/end 記録。"""

from __future__ import annotations

from time import perf_counter
from typing import TYPE_CHECKING, Protocol

import logfire

from app.agent.recording.types import PhaseCall, PhaseStatus

if TYPE_CHECKING:
    from app.agent.evidence_review.metrics import EvidenceReviewOutcome

__all__ = [
    "EvidenceReviewRecorder",
    "LogfireEvidenceReviewRecorder",
    "logfire_evidence_review_recorder",
]

_DURATION_METRIC = "vector.agent.evidence_review.duration"
_MISSING_OUTCOME = "none"

_duration_histogram = logfire.metric_histogram(
    _DURATION_METRIC,
    unit="s",
    description="Evidence review process duration",
)


class EvidenceReviewRecorder(Protocol):
    """start は必ず PhaseCall を返し、記録の例外は本処理へ出さない。"""

    async def start(self) -> PhaseCall: ...

    async def end(
        self,
        call: PhaseCall,
        *,
        outcome: EvidenceReviewOutcome | None = None,
        retry_used: bool = False,
        stopped: bool = False,
    ) -> None: ...


def _status_from_result(
    *,
    stopped: bool,
    outcome: EvidenceReviewOutcome | None,
) -> PhaseStatus:
    """工程の終わり方を記録の status に写す。"""

    if stopped:
        return PhaseStatus.STOPPED
    if outcome == "completed":
        return PhaseStatus.COMPLETED
    return PhaseStatus.FAILED


class LogfireEvidenceReviewRecorder:
    async def start(self) -> PhaseCall:
        try:
            return PhaseCall(started_at=perf_counter())
        except Exception:
            return PhaseCall(started_at=0.0)

    async def end(
        self,
        call: PhaseCall,
        *,
        outcome: EvidenceReviewOutcome | None = None,
        retry_used: bool = False,
        stopped: bool = False,
    ) -> None:
        try:
            status = _status_from_result(stopped=stopped, outcome=outcome)
            attributes = {
                "status": status.value,
                "outcome": (
                    _MISSING_OUTCOME if stopped or outcome is None else outcome
                ),
            }
            _duration_histogram.record(
                perf_counter() - call.started_at,
                attributes=attributes,
            )
            if stopped or outcome is None:
                return
            from app.agent.evidence_review.metrics import (
                record_evidence_review_outcome,
            )

            record_evidence_review_outcome(
                result=outcome,
                retry_used=retry_used,
            )
        except Exception:
            return


logfire_evidence_review_recorder = LogfireEvidenceReviewRecorder()
