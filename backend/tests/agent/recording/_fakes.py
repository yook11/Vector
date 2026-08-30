"""Recorder の記録を蓄積する test double。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, field

from app.agent.recording.direct_answer import DirectAnswerOutcome
from app.agent.recording.evidence_answer import EvidenceAnswerRecordingOutcome
from app.agent.recording.evidence_collection import EvidenceCollectionRecording
from app.agent.recording.evidence_review import EvidenceReviewOutcome
from app.agent.recording.external_search import ExternalSearchOutcome
from app.agent.recording.internal_search import InternalSearchRecordingOutcome
from app.agent.recording.llm import LlmCallMode
from app.agent.recording.planning import PlanningOutcome
from app.agent.recording.types import Usage
from app.agent.runtime.llm_failure import LlmAttemptFailed

__all__ = [
    "RecordedDirectAnswer",
    "RecordedEvidenceAnswer",
    "RecordedEvidenceCollection",
    "RecordedEvidenceCollectionTask",
    "RecordedEvidenceReview",
    "RecordedExternalSearch",
    "RecordedExternalSearchQueryGeneration",
    "RecordedInternalSearch",
    "RecordedLlmCall",
    "RecordedPlanning",
    "RecordingDirectAnswerRecorder",
    "RecordingEvidenceAnswerRecorder",
    "RecordingEvidenceCollectionRecorder",
    "RecordingEvidenceReviewRecorder",
    "RecordingExternalSearchRecorder",
    "RecordingInternalSearchRecorder",
    "RecordingLlmCallRecorder",
    "RecordingPlanningRecorder",
]


@dataclass(slots=True)
class RecordedLlmCall:
    agent_name: str
    provider: str
    model: str
    attempt_number: int
    prompt_version: str
    operation_name: str
    gen_ai_provider: str
    mode: LlmCallMode
    parent_context: object | None = None
    usage: Usage | None = None
    failure: LlmAttemptFailed | None = None
    span_result: str | None = None
    error: BaseException | None = None

    def report_usage(self, usage: Usage) -> None:
        self.usage = usage

    def report_outcome(
        self,
        failure: LlmAttemptFailed,
        *,
        span_result: str,
    ) -> None:
        self.failure = failure
        self.span_result = span_result


@dataclass(slots=True)
class RecordingLlmCallRecorder:
    records: list[RecordedLlmCall] = field(default_factory=list)

    def record(
        self,
        *,
        agent_name: str,
        provider: str,
        model: str,
        attempt_number: int,
        prompt_version: str,
        operation_name: str,
        gen_ai_provider: str,
        mode: LlmCallMode,
        parent_context: object | None = None,
    ) -> AbstractAsyncContextManager[RecordedLlmCall]:
        return self._record(
            agent_name=agent_name,
            provider=provider,
            model=model,
            attempt_number=attempt_number,
            prompt_version=prompt_version,
            operation_name=operation_name,
            gen_ai_provider=gen_ai_provider,
            mode=mode,
            parent_context=parent_context,
        )

    @asynccontextmanager
    async def _record(
        self,
        *,
        agent_name: str,
        provider: str,
        model: str,
        attempt_number: int,
        prompt_version: str,
        operation_name: str,
        gen_ai_provider: str,
        mode: LlmCallMode,
        parent_context: object | None,
    ) -> AsyncIterator[RecordedLlmCall]:
        recording = RecordedLlmCall(
            agent_name=agent_name,
            provider=provider,
            model=model,
            attempt_number=attempt_number,
            prompt_version=prompt_version,
            operation_name=operation_name,
            gen_ai_provider=gen_ai_provider,
            mode=mode,
            parent_context=parent_context,
        )
        self.records.append(recording)
        try:
            yield recording
        except BaseException as error:
            recording.error = error
            raise


@dataclass(slots=True)
class RecordedInternalSearch:
    query_count: int
    outcomes: list[InternalSearchRecordingOutcome] = field(default_factory=list)
    error: BaseException | None = None

    def report_outcome(self, outcome: InternalSearchRecordingOutcome) -> None:
        self.outcomes.append(outcome)


@dataclass(slots=True)
class RecordingInternalSearchRecorder:
    records: list[RecordedInternalSearch] = field(default_factory=list)

    def record(
        self,
        *,
        query_count: int,
    ) -> AbstractAsyncContextManager[RecordedInternalSearch]:
        return self._record(query_count=query_count)

    @asynccontextmanager
    async def _record(
        self,
        *,
        query_count: int,
    ) -> AsyncIterator[RecordedInternalSearch]:
        recording = RecordedInternalSearch(query_count=query_count)
        self.records.append(recording)
        try:
            yield recording
        except BaseException as error:
            recording.error = error
            raise


@dataclass(slots=True)
class RecordedExternalSearchQueryGeneration:
    agent_name: str
    error: BaseException | None = None


@dataclass(slots=True)
class RecordedExternalSearch:
    outcomes: list[ExternalSearchOutcome] = field(default_factory=list)
    query_generations: list[RecordedExternalSearchQueryGeneration] = field(
        default_factory=list
    )
    error: BaseException | None = None

    def report_outcome(self, outcome: ExternalSearchOutcome) -> None:
        self.outcomes.append(outcome)

    def record_query_generation(
        self,
        *,
        agent_name: str,
    ) -> AbstractAsyncContextManager[None]:
        return self._record_query_generation(agent_name=agent_name)

    @asynccontextmanager
    async def _record_query_generation(self, *, agent_name: str) -> AsyncIterator[None]:
        recording = RecordedExternalSearchQueryGeneration(agent_name=agent_name)
        self.query_generations.append(recording)
        try:
            yield
        except BaseException as error:
            recording.error = error
            raise


@dataclass(slots=True)
class RecordingExternalSearchRecorder:
    records: list[RecordedExternalSearch] = field(default_factory=list)

    def record(self) -> AbstractAsyncContextManager[RecordedExternalSearch]:
        return self._record()

    @asynccontextmanager
    async def _record(self) -> AsyncIterator[RecordedExternalSearch]:
        recording = RecordedExternalSearch()
        self.records.append(recording)
        try:
            yield recording
        except BaseException as error:
            recording.error = error
            raise


@dataclass(slots=True)
class RecordedEvidenceCollectionTask:
    task_index: int
    error: BaseException | None = None


@dataclass(slots=True)
class RecordedEvidenceCollection(EvidenceCollectionRecording):
    tasks: list[RecordedEvidenceCollectionTask] = field(default_factory=list)
    error: BaseException | None = None

    def record_task(
        self,
        *,
        task_index: int,
    ) -> AbstractAsyncContextManager[None]:
        return self._record_task(task_index=task_index)

    @asynccontextmanager
    async def _record_task(self, *, task_index: int) -> AsyncIterator[None]:
        task = RecordedEvidenceCollectionTask(task_index=task_index)
        self.tasks.append(task)
        try:
            yield
        except BaseException as error:
            task.error = error
            raise


@dataclass(slots=True)
class RecordingEvidenceCollectionRecorder:
    records: list[RecordedEvidenceCollection] = field(default_factory=list)

    def record(self) -> AbstractAsyncContextManager[RecordedEvidenceCollection]:
        return self._record()

    @asynccontextmanager
    async def _record(self) -> AsyncIterator[RecordedEvidenceCollection]:
        recording = RecordedEvidenceCollection()
        self.records.append(recording)
        try:
            yield recording
        except BaseException as error:
            recording.error = error
            raise


@dataclass(slots=True)
class RecordedPlanning:
    agent_name: str
    outcomes: list[PlanningOutcome] = field(default_factory=list)
    error: BaseException | None = None

    def report_outcome(self, outcome: PlanningOutcome) -> None:
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

    def report_outcome(self, outcome: DirectAnswerOutcome) -> None:
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

    def report_outcome(self, outcome: EvidenceAnswerRecordingOutcome) -> None:
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


@dataclass(slots=True)
class RecordedEvidenceReview:
    agent_name: str
    outcomes: list[EvidenceReviewOutcome] = field(default_factory=list)
    error: BaseException | None = None

    def report_outcome(self, outcome: EvidenceReviewOutcome) -> None:
        self.outcomes.append(outcome)


@dataclass(slots=True)
class RecordingEvidenceReviewRecorder:
    records: list[RecordedEvidenceReview] = field(default_factory=list)

    def record(
        self,
        *,
        agent_name: str,
    ) -> AbstractAsyncContextManager[RecordedEvidenceReview]:
        return self._record(agent_name=agent_name)

    @asynccontextmanager
    async def _record(
        self,
        *,
        agent_name: str,
    ) -> AsyncIterator[RecordedEvidenceReview]:
        recording = RecordedEvidenceReview(agent_name=agent_name)
        self.records.append(recording)
        try:
            yield recording
        except BaseException as error:
            recording.error = error
            raise
