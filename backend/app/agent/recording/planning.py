"""計画工程 plan() 1回の start/end 記録。"""

from __future__ import annotations

from time import perf_counter
from typing import TYPE_CHECKING, Literal, Protocol

import logfire

from app.agent.contract import PlanType
from app.agent.recording.types import PhaseCall, PhaseStatus

if TYPE_CHECKING:
    from app.agent.planning.metrics import PlannerOutcomeResult

__all__ = [
    "LogfirePlanningRecorder",
    "PlanningRecorder",
    "logfire_planning_recorder",
]

_DURATION_METRIC = "vector.agent.planner.duration"
_MISSING_OUTCOME = "none"

_duration_histogram = logfire.metric_histogram(
    _DURATION_METRIC,
    unit="s",
    description="Question planner process duration",
)


class PlanningRecorder(Protocol):
    """start は必ず PhaseCall を返し、記録の例外は本処理へ出さない。"""

    async def start(self) -> PhaseCall: ...

    async def end(
        self,
        call: PhaseCall,
        *,
        outcome: PlannerOutcomeResult | None = None,
        retry_used: bool = False,
        plan_type: PlanType | Literal["not_created"] | None = None,
        failure_code: str | None = None,
        stopped: bool = False,
    ) -> None: ...


def _status_from_result(
    *,
    stopped: bool,
    outcome: PlannerOutcomeResult | None,
) -> PhaseStatus:
    """工程の終わり方を記録の status に写す。"""

    if stopped:
        return PhaseStatus.STOPPED
    if outcome == "planned":
        return PhaseStatus.COMPLETED
    return PhaseStatus.FAILED


class LogfirePlanningRecorder:
    async def start(self) -> PhaseCall:
        try:
            return PhaseCall(started_at=perf_counter())
        except Exception:
            return PhaseCall(started_at=0.0)

    async def end(
        self,
        call: PhaseCall,
        *,
        outcome: PlannerOutcomeResult | None = None,
        retry_used: bool = False,
        plan_type: PlanType | Literal["not_created"] | None = None,
        failure_code: str | None = None,
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
            if stopped or outcome is None or plan_type is None:
                return
            from app.agent.planning.metrics import record_question_planner_outcome

            record_question_planner_outcome(
                result=outcome,
                retry_used=retry_used,
                plan_type=plan_type,
                failure_code=failure_code,
            )
        except Exception:
            return


logfire_planning_recorder = LogfirePlanningRecorder()
