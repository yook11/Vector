"""根拠回答工程 answer() 1回の start/end 記録。"""

from __future__ import annotations

from time import perf_counter
from typing import TYPE_CHECKING, Protocol

import logfire

from app.agent.recording.types import PhaseCall, PhaseStatus

if TYPE_CHECKING:
    from app.agent.answering.metrics import AnswerSynthesisOutcomeResult

__all__ = [
    "EvidenceAnswerRecorder",
    "LogfireEvidenceAnswerRecorder",
    "logfire_evidence_answer_recorder",
]

_DURATION_METRIC = "vector.agent.answer_synthesis.duration"
_MISSING_OUTCOME = "none"

_duration_histogram = logfire.metric_histogram(
    _DURATION_METRIC,
    unit="s",
    description="Evidence answer synthesis process duration",
)


class EvidenceAnswerRecorder(Protocol):
    """start は必ず PhaseCall を返し、記録の例外は本処理へ出さない。"""

    async def start(self) -> PhaseCall: ...

    async def end(
        self,
        call: PhaseCall,
        *,
        outcome: AnswerSynthesisOutcomeResult | None = None,
        retry_used: bool = False,
        fallback_used: bool = False,
        failure_code: str | None = None,
        stopped: bool = False,
    ) -> None: ...


def _status_from_result(
    *,
    stopped: bool,
    outcome: AnswerSynthesisOutcomeResult | None,
) -> PhaseStatus:
    """工程の終わり方を記録の status に写す。"""

    if stopped:
        return PhaseStatus.STOPPED
    if outcome == "synthesized":
        return PhaseStatus.COMPLETED
    return PhaseStatus.FAILED


class LogfireEvidenceAnswerRecorder:
    async def start(self) -> PhaseCall:
        try:
            return PhaseCall(started_at=perf_counter())
        except Exception:
            return PhaseCall(started_at=0.0)

    async def end(
        self,
        call: PhaseCall,
        *,
        outcome: AnswerSynthesisOutcomeResult | None = None,
        retry_used: bool = False,
        fallback_used: bool = False,
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
            if stopped or outcome is None:
                return
            from app.agent.answering.metrics import record_answer_synthesis_outcome

            record_answer_synthesis_outcome(
                result=outcome,
                retry_used=retry_used,
                fallback_used=fallback_used,
                failure_code=failure_code,
            )
        except Exception:
            return


logfire_evidence_answer_recorder = LogfireEvidenceAnswerRecorder()
