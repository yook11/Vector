"""Validated direct answer service."""

from __future__ import annotations

from app.agent.agent import Agent
from app.agent.answering.contract import AnsweringRequest
from app.agent.answering.direct_answer.contract import (
    DirectAnswerDraft,
    DirectAnswerInput,
    DirectAnswerInvalidError,
)
from app.agent.answering.direct_answer.failure import DirectAnswerError
from app.agent.answering.failure import (
    RequestRetryDisposition,
    classify_direct_answer_failure,
)
from app.agent.answering.live_delivery import (
    BestEffortAnswerDeltaReporter,
    close_answer_stream,
    ensure_answer_generation_continues,
)
from app.agent.answering.live_draft import LiveAnswerDraftSession
from app.agent.citation_markers import strip_citation_markers
from app.agent.contract import (
    AnswerDeltaReporter,
    AnswerGenerationContinuation,
)
from app.agent.recording.direct_answer import (
    DirectAnswerFailed,
    DirectAnswerRecorder,
    DirectAnswerSucceeded,
    logfire_direct_answer_recorder,
)
from app.agent.runtime.contract import (
    AgentTextStream,
    StreamingAgentRuntime,
    StreamingAgentRuntimeScopeFactory,
)
from app.analysis.ai_provider_errors import (
    AIProviderError,
    AIProviderOutputTruncatedError,
)

__all__ = ["DirectAnswerService"]

_DIRECT_ANSWER_SOURCE_ERRORS = (AIProviderError, DirectAnswerInvalidError)
_MAX_ATTEMPTS = 2


class DirectAnswerService:
    """Create validated direct answer drafts.

    Propagates classified DirectAnswerError or routine generation-stop signals.
    """

    def __init__(
        self,
        *,
        agent: Agent[DirectAnswerInput, DirectAnswerDraft],
        runtime_scope_factory: StreamingAgentRuntimeScopeFactory,
        delta_reporter: AnswerDeltaReporter | None = None,
        continuation: AnswerGenerationContinuation | None = None,
        recorder: DirectAnswerRecorder = logfire_direct_answer_recorder,
    ) -> None:
        self._agent = agent
        self._runtime_scope_factory = runtime_scope_factory
        self._delta = BestEffortAnswerDeltaReporter(delta_reporter)
        self._continuation = continuation
        self._recorder = recorder

    async def answer(
        self,
        *,
        request: AnsweringRequest,
        previous_answer: str = "",
    ) -> DirectAnswerDraft:
        """Return a valid direct draft, retrying only blank response defects."""

        repair_context: str | None = None
        previous_output_truncated = False
        completed_draft: DirectAnswerDraft | None = None
        attempt_count = 0

        async with self._recorder.record(agent_name=self._agent.name) as recording:
            try:
                async with self._runtime_scope_factory() as runtime:
                    for attempt_number in range(1, _MAX_ATTEMPTS + 1):
                        attempt_count = attempt_number
                        try:
                            completed_draft = await self._generate_draft(
                                runtime=runtime,
                                request=request,
                                previous_answer=previous_answer,
                                repair_context=repair_context,
                                previous_output_truncated=previous_output_truncated,
                                attempt_number=attempt_number,
                            )
                        except _DIRECT_ANSWER_SOURCE_ERRORS as cause:
                            failure = classify_direct_answer_failure(cause)
                            retriable = (
                                failure.request_retry_disposition
                                is RequestRetryDisposition.RETRY_IN_REQUEST
                                and attempt_number < _MAX_ATTEMPTS
                            )
                            if retriable:
                                repair_context = str(cause)
                                previous_output_truncated = isinstance(
                                    cause, AIProviderOutputTruncatedError
                                )
                                continue
                            raise DirectAnswerError(code=failure.code) from cause
                        break
            except DirectAnswerError as error:
                recording.set_outcome(
                    DirectAnswerFailed(
                        failure_code=error.code,
                        attempt_count=attempt_count,
                    )
                )
                raise

            if completed_draft is not None:
                recording.set_outcome(
                    DirectAnswerSucceeded(attempt_count=attempt_count)
                )
                return completed_draft

            raise AssertionError("unreachable: answer loop must return or raise")

    async def _generate_draft(
        self,
        *,
        runtime: StreamingAgentRuntime,
        request: AnsweringRequest,
        previous_answer: str,
        repair_context: str | None,
        previous_output_truncated: bool,
        attempt_number: int,
    ) -> DirectAnswerDraft:
        stream: AgentTextStream | None = None
        raw_fragments: list[str] = []
        try:
            async with LiveAnswerDraftSession(
                generation=attempt_number,
                delta_reporter=self._delta,
            ) as live_draft:
                await ensure_answer_generation_continues(self._continuation)

                stream = runtime.stream_text(
                    self._agent,
                    DirectAnswerInput(
                        request=request,
                        previous_answer=previous_answer,
                        repair_context=repair_context,
                        previous_output_truncated=previous_output_truncated,
                    ),
                    attempt_number=attempt_number,
                )
                async for fragment in stream:
                    await ensure_answer_generation_continues(self._continuation)
                    raw_fragments.append(fragment)
                    await live_draft.append(fragment)

                await ensure_answer_generation_continues(self._continuation)
                answer = strip_citation_markers("".join(raw_fragments))
                if not answer.strip():
                    raise DirectAnswerInvalidError()
                draft = self._agent.output_type(answer=answer)

                await live_draft.commit()
                return draft
        finally:
            await close_answer_stream(stream)
