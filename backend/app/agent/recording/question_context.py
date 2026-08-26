"""文脈準備工程 prepare() 1回の start/end 記録。"""

from __future__ import annotations

from time import perf_counter
from typing import TYPE_CHECKING, Protocol

import logfire

from app.agent.recording.types import PhaseCall, PhaseStatus

if TYPE_CHECKING:
    from app.agent.question_context.metrics import QuestionContextOutcome

__all__ = [
    "LogfireQuestionContextRecorder",
    "QuestionContextRecorder",
    "logfire_question_context_recorder",
]

_DURATION_METRIC = "vector.agent.question_context.duration"
_MISSING_OUTCOME = "none"

_duration_histogram = logfire.metric_histogram(
    _DURATION_METRIC,
    unit="s",
    description="Question context process duration",
)


class QuestionContextRecorder(Protocol):
    """start は必ず PhaseCall を返し、記録の例外は本処理へ出さない。"""

    async def start(self) -> PhaseCall: ...

    async def end(
        self,
        call: PhaseCall,
        *,
        outcome: QuestionContextOutcome | None = None,
        prompt_version: str | None = None,
        ai_model: str | None = None,
        failure_code: str | None = None,
        stopped: bool = False,
    ) -> None: ...


def _status_from_result(
    *,
    stopped: bool,
    outcome: QuestionContextOutcome | None,
) -> PhaseStatus:
    """工程の終わり方を記録の status に写す。"""

    if stopped:
        return PhaseStatus.STOPPED
    if outcome == "prepared":
        return PhaseStatus.COMPLETED
    return PhaseStatus.FAILED


class LogfireQuestionContextRecorder:
    async def start(self) -> PhaseCall:
        try:
            return PhaseCall(started_at=perf_counter())
        except Exception:
            return PhaseCall(started_at=0.0)

    async def end(
        self,
        call: PhaseCall,
        *,
        outcome: QuestionContextOutcome | None = None,
        prompt_version: str | None = None,
        ai_model: str | None = None,
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
            if stopped or outcome is None or prompt_version is None or ai_model is None:
                return
            from app.agent.question_context.metrics import (
                record_question_context_outcome,
            )

            record_question_context_outcome(
                result=outcome,
                prompt_version=prompt_version,
                ai_model=ai_model,
                failure_code=failure_code,
            )
        except Exception:
            return


logfire_question_context_recorder = LogfireQuestionContextRecorder()
