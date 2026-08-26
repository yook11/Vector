"""Recorder の記録を蓄積する test double。"""

from __future__ import annotations

from dataclasses import dataclass, field
from time import perf_counter
from typing import Literal

from app.agent.contract import PlanType
from app.agent.evidence_collection.contract import TaskExternalCollectionStatus
from app.agent.evidence_collection.internal_search.contract import (
    InternalSearchFailurePhase,
    InternalSearchOutcome,
)
from app.agent.planning.metrics import PlannerOutcomeResult
from app.agent.question_context.metrics import QuestionContextOutcome
from app.agent.recording.types import LlmCall, LlmCallResult, PhaseCall, Usage

__all__ = [
    "RecordedExternalSearchEnd",
    "RecordedInternalSearchEnd",
    "RecordedLlmCallEnd",
    "RecordedPlanningEnd",
    "RecordedQuestionContextEnd",
    "RecordingExternalSearchRecorder",
    "RecordingInternalSearchRecorder",
    "RecordingLlmCallRecorder",
    "RecordingPlanningRecorder",
    "RecordingQuestionContextRecorder",
]


@dataclass(frozen=True, slots=True)
class RecordedLlmCallEnd:
    call: LlmCall
    result: LlmCallResult | None
    usage: Usage | None
    stopped: bool


@dataclass(slots=True)
class RecordingLlmCallRecorder:
    starts: list[LlmCall] = field(default_factory=list)
    ends: list[RecordedLlmCallEnd] = field(default_factory=list)

    def start(
        self,
        *,
        agent_name: str,
        provider: str,
        model: str,
        attempt_number: int,
    ) -> LlmCall:
        call = LlmCall(
            agent_name=agent_name,
            provider=provider,
            model=model,
            attempt_number=attempt_number,
            started_at=perf_counter(),
        )
        self.starts.append(call)
        return call

    def end(
        self,
        call: LlmCall,
        *,
        result: LlmCallResult | None = None,
        usage: Usage | None = None,
        stopped: bool = False,
    ) -> None:
        self.ends.append(
            RecordedLlmCallEnd(
                call=call,
                result=result,
                usage=usage,
                stopped=stopped,
            )
        )


@dataclass(frozen=True, slots=True)
class RecordedInternalSearchEnd:
    call: PhaseCall
    outcome: InternalSearchOutcome | None
    query_count: int
    failure_phase: InternalSearchFailurePhase | None
    stopped: bool


@dataclass(slots=True)
class RecordingInternalSearchRecorder:
    starts: list[PhaseCall] = field(default_factory=list)
    ends: list[RecordedInternalSearchEnd] = field(default_factory=list)

    async def start(self) -> PhaseCall:
        call = PhaseCall(started_at=perf_counter())
        self.starts.append(call)
        return call

    async def end(
        self,
        call: PhaseCall,
        *,
        query_count: int,
        outcome: InternalSearchOutcome | None = None,
        failure_phase: InternalSearchFailurePhase | None = None,
        stopped: bool = False,
    ) -> None:
        self.ends.append(
            RecordedInternalSearchEnd(
                call=call,
                outcome=outcome,
                query_count=query_count,
                failure_phase=failure_phase,
                stopped=stopped,
            )
        )


@dataclass(frozen=True, slots=True)
class RecordedExternalSearchEnd:
    call: PhaseCall
    outcome: TaskExternalCollectionStatus | None
    stopped: bool


@dataclass(slots=True)
class RecordingExternalSearchRecorder:
    starts: list[PhaseCall] = field(default_factory=list)
    ends: list[RecordedExternalSearchEnd] = field(default_factory=list)

    async def start(self) -> PhaseCall:
        call = PhaseCall(started_at=perf_counter())
        self.starts.append(call)
        return call

    async def end(
        self,
        call: PhaseCall,
        *,
        outcome: TaskExternalCollectionStatus | None = None,
        stopped: bool = False,
    ) -> None:
        self.ends.append(
            RecordedExternalSearchEnd(
                call=call,
                outcome=outcome,
                stopped=stopped,
            )
        )


@dataclass(frozen=True, slots=True)
class RecordedPlanningEnd:
    call: PhaseCall
    outcome: PlannerOutcomeResult | None
    retry_used: bool
    plan_type: PlanType | Literal["not_created"] | None
    failure_code: str | None
    stopped: bool


@dataclass(slots=True)
class RecordingPlanningRecorder:
    starts: list[PhaseCall] = field(default_factory=list)
    ends: list[RecordedPlanningEnd] = field(default_factory=list)

    async def start(self) -> PhaseCall:
        call = PhaseCall(started_at=perf_counter())
        self.starts.append(call)
        return call

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
        self.ends.append(
            RecordedPlanningEnd(
                call=call,
                outcome=outcome,
                retry_used=retry_used,
                plan_type=plan_type,
                failure_code=failure_code,
                stopped=stopped,
            )
        )


@dataclass(frozen=True, slots=True)
class RecordedQuestionContextEnd:
    call: PhaseCall
    outcome: QuestionContextOutcome | None
    prompt_version: str | None
    ai_model: str | None
    failure_code: str | None
    stopped: bool


@dataclass(slots=True)
class RecordingQuestionContextRecorder:
    starts: list[PhaseCall] = field(default_factory=list)
    ends: list[RecordedQuestionContextEnd] = field(default_factory=list)

    async def start(self) -> PhaseCall:
        call = PhaseCall(started_at=perf_counter())
        self.starts.append(call)
        return call

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
        self.ends.append(
            RecordedQuestionContextEnd(
                call=call,
                outcome=outcome,
                prompt_version=prompt_version,
                ai_model=ai_model,
                failure_code=failure_code,
                stopped=stopped,
            )
        )
