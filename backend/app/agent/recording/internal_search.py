"""内部検索 search() 1回の start/end 記録。"""

from __future__ import annotations

from time import perf_counter
from typing import TYPE_CHECKING, Protocol

import logfire

from app.agent.recording.types import PhaseCall, PhaseStatus

if TYPE_CHECKING:
    from app.agent.evidence_collection.internal_search.contract import (
        InternalSearchFailurePhase,
        InternalSearchOutcome,
    )

__all__ = [
    "InternalSearchRecorder",
    "LogfireInternalSearchRecorder",
    "logfire_internal_search_recorder",
]

_DURATION_METRIC = "vector.agent.internal_search.duration"
_MISSING_OUTCOME = "none"

_duration_histogram = logfire.metric_histogram(
    _DURATION_METRIC,
    unit="s",
    description="Internal search process duration",
)


class InternalSearchRecorder(Protocol):
    """start は必ず PhaseCall を返し、記録の例外は本処理へ出さない。"""

    async def start(self) -> PhaseCall: ...

    async def end(
        self,
        call: PhaseCall,
        *,
        query_count: int,
        outcome: InternalSearchOutcome | None = None,
        failure_phase: InternalSearchFailurePhase | None = None,
        stopped: bool = False,
    ) -> None: ...


def _status_from_result(
    *,
    stopped: bool,
    outcome: InternalSearchOutcome | None,
) -> PhaseStatus:
    """工程の終わり方を記録の status に写す。"""

    if stopped:
        return PhaseStatus.STOPPED
    if outcome in ("succeeded", "empty"):
        return PhaseStatus.COMPLETED
    return PhaseStatus.FAILED


class LogfireInternalSearchRecorder:
    async def start(self) -> PhaseCall:
        try:
            return PhaseCall(started_at=perf_counter())
        except Exception:
            return PhaseCall(started_at=0.0)

    async def end(
        self,
        call: PhaseCall,
        *,
        query_count: int,
        outcome: InternalSearchOutcome | None = None,
        failure_phase: InternalSearchFailurePhase | None = None,
        stopped: bool = False,
    ) -> None:
        try:
            status = _status_from_result(stopped=stopped, outcome=outcome)
            attributes = {
                "status": status.value,
                "outcome": outcome if outcome is not None else _MISSING_OUTCOME,
            }
            _duration_histogram.record(
                perf_counter() - call.started_at,
                attributes=attributes,
            )
            if outcome is None:
                return
            from app.agent.evidence_collection.internal_search.metrics import (
                record_internal_retrieval_outcome,
            )

            record_internal_retrieval_outcome(
                result=outcome,
                query_count=query_count,
                failure_phase=failure_phase,
            )
        except Exception:
            return


logfire_internal_search_recorder = LogfireInternalSearchRecorder()
