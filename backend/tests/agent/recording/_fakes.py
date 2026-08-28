"""Recorder の記録を蓄積する test double。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, field
from time import perf_counter

from app.agent.evidence_collection.contract import TaskExternalCollectionStatus
from app.agent.evidence_collection.internal_search.contract import (
    InternalSearchFailurePhase,
    InternalSearchOutcome,
)
from app.agent.evidence_review.metrics import EvidenceReviewOutcome
from app.agent.recording.direct_answer import DirectAnswerOutcome
from app.agent.recording.evidence_answer import EvidenceAnswerRecordingOutcome
from app.agent.recording.planning import PlanningOutcome
from app.agent.recording.types import LlmCall, LlmCallResult, PhaseCall, Usage

__all__ = [
    "RecordedDirectAnswer",
    "RecordedEvidenceAnswer",
    "RecordedEvidenceReviewEnd",
    "RecordedExternalSearchEnd",
    "RecordedInternalSearchEnd",
    "RecordedLlmCallEnd",
    "RecordedPlanning",
    "RecordingDirectAnswerRecorder",
    "RecordingEvidenceAnswerRecorder",
    "RecordingEvidenceReviewRecorder",
    "RecordingExternalSearchRecorder",
    "RecordingInternalSearchRecorder",
    "RecordingLlmCallRecorder",
    "RecordingPlanningRecorder",
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


@dataclass(slots=True)
class RecordedPlanning:
    agent_name: str
    outcomes: list[PlanningOutcome] = field(default_factory=list)
    error: BaseException | None = None

    def set_outcome(self, outcome: PlanningOutcome) -> None:
        self.outcomes.append(outcome)


@dataclass(slots=True)
class RecordingPlanningRecorder:
    records: list[RecordedPlanning] = field(default_factory=list)

    def record(
        self,
        *,
        agent_name: str,
    ) -> AbstractAsyncContextManager[RecordedPlanning]:
        return self._record(agent_name=agent_name)

    @asynccontextmanager
    async def _record(self, *, agent_name: str) -> AsyncIterator[RecordedPlanning]:
        recording = RecordedPlanning(agent_name=agent_name)
        self.records.append(recording)
        try:
            yield recording
        except BaseException as error:
            recording.error = error
            raise


@dataclass(slots=True)
class RecordedDirectAnswer:
    agent_name: str
    outcomes: list[DirectAnswerOutcome] = field(default_factory=list)
    error: BaseException | None = None

    def set_outcome(self, outcome: DirectAnswerOutcome) -> None:
        self.outcomes.append(outcome)


@dataclass(slots=True)
class RecordingDirectAnswerRecorder:
    records: list[RecordedDirectAnswer] = field(default_factory=list)

    def record(
        self,
        *,
        agent_name: str,
    ) -> AbstractAsyncContextManager[RecordedDirectAnswer]:
        return self._record(agent_name=agent_name)

    @asynccontextmanager
    async def _record(
        self,
        *,
        agent_name: str,
    ) -> AsyncIterator[RecordedDirectAnswer]:
        recording = RecordedDirectAnswer(agent_name=agent_name)
        self.records.append(recording)
        try:
            yield recording
        except BaseException as error:
            recording.error = error
            raise


@dataclass(slots=True)
class RecordedEvidenceAnswer:
    agent_name: str
    outcomes: list[EvidenceAnswerRecordingOutcome] = field(default_factory=list)
    error: BaseException | None = None

    def set_outcome(self, outcome: EvidenceAnswerRecordingOutcome) -> None:
        self.outcomes.append(outcome)


@dataclass(slots=True)
class RecordingEvidenceAnswerRecorder:
    records: list[RecordedEvidenceAnswer] = field(default_factory=list)

    def record(
        self,
        *,
        agent_name: str,
    ) -> AbstractAsyncContextManager[RecordedEvidenceAnswer]:
        return self._record(agent_name=agent_name)

    @asynccontextmanager
    async def _record(
        self,
        *,
        agent_name: str,
    ) -> AsyncIterator[RecordedEvidenceAnswer]:
        recording = RecordedEvidenceAnswer(agent_name=agent_name)
        self.records.append(recording)
        try:
            yield recording
        except BaseException as error:
            recording.error = error
            raise


@dataclass(frozen=True, slots=True)
class RecordedEvidenceReviewEnd:
    call: PhaseCall
    outcome: EvidenceReviewOutcome | None
    retry_used: bool
    stopped: bool


@dataclass(slots=True)
class RecordingEvidenceReviewRecorder:
    starts: list[PhaseCall] = field(default_factory=list)
    ends: list[RecordedEvidenceReviewEnd] = field(default_factory=list)

    async def start(self) -> PhaseCall:
        call = PhaseCall(started_at=perf_counter())
        self.starts.append(call)
        return call

    async def end(
        self,
        call: PhaseCall,
        *,
        outcome: EvidenceReviewOutcome | None = None,
        retry_used: bool = False,
        stopped: bool = False,
    ) -> None:
        self.ends.append(
            RecordedEvidenceReviewEnd(
                call=call,
                outcome=outcome,
                retry_used=retry_used,
                stopped=stopped,
            )
        )
