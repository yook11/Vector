"""外部検索 search() 1回の start/end 記録。"""

from __future__ import annotations

from time import perf_counter
from typing import TYPE_CHECKING, Protocol

import logfire

from app.agent.recording.types import PhaseCall, PhaseStatus

if TYPE_CHECKING:
    from app.agent.evidence_collection.contract import TaskExternalCollectionStatus

__all__ = [
    "ExternalSearchRecorder",
    "LogfireExternalSearchRecorder",
    "logfire_external_search_recorder",
]

_OUTCOME_METRIC = "vector.agent.external_search.outcome"
_DURATION_METRIC = "vector.agent.external_search.duration"
_MISSING_OUTCOME = "none"

_outcome_counter = logfire.metric_counter(
    _OUTCOME_METRIC,
    unit="1",
    description="External search process outcome",
)
_duration_histogram = logfire.metric_histogram(
    _DURATION_METRIC,
    unit="s",
    description="External search process duration",
)


class ExternalSearchRecorder(Protocol):
    """start は必ず PhaseCall を返し、記録の例外は本処理へ出さない。"""

    async def start(self) -> PhaseCall: ...

    async def end(
        self,
        call: PhaseCall,
        *,
        outcome: TaskExternalCollectionStatus | None = None,
        stopped: bool = False,
    ) -> None: ...


def _status_from_result(
    *,
    stopped: bool,
    outcome: TaskExternalCollectionStatus | None,
) -> PhaseStatus:
    """工程の終わり方を記録の status に写す。"""

    if stopped:
        return PhaseStatus.STOPPED
    if outcome == "succeeded":
        return PhaseStatus.COMPLETED
    return PhaseStatus.FAILED


class LogfireExternalSearchRecorder:
    async def start(self) -> PhaseCall:
        try:
            return PhaseCall(started_at=perf_counter())
        except Exception:
            return PhaseCall(started_at=0.0)

    async def end(
        self,
        call: PhaseCall,
        *,
        outcome: TaskExternalCollectionStatus | None = None,
        stopped: bool = False,
    ) -> None:
        try:
            status = _status_from_result(stopped=stopped, outcome=outcome)
            attributes = {
                "status": status.value,
                "outcome": outcome if outcome is not None else _MISSING_OUTCOME,
            }
            _outcome_counter.add(1, attributes=attributes)
            _duration_histogram.record(
                perf_counter() - call.started_at,
                attributes=attributes,
            )
        except Exception:
            return


logfire_external_search_recorder = LogfireExternalSearchRecorder()
