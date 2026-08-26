"""Validated direct answer flow."""

from __future__ import annotations

import asyncio

from app.agent.agent import Agent
from app.agent.answering.contract import AnsweringRequest
from app.agent.answering.direct_answer.contract import (
    DirectAnswerDraft,
    DirectAnswerInput,
    DirectAnswerInvalidError,
)
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
from app.agent.answering.metrics import DirectAnswerOutcomeResult
from app.agent.citation_markers import strip_citation_markers
from app.agent.contract import (
    AnswerDeltaReporter,
    AnswerGenerationContinuation,
    AnswerGenerationStopped,
)
from app.agent.phase_span import agent_phase
from app.agent.recording.direct_answer import (
    DirectAnswerRecorder,
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

__all__ = ["DirectAnswerFlow"]

_DIRECT_ANSWER_FAILURES = (AIProviderError, DirectAnswerInvalidError)
_MAX_ATTEMPTS = 2


class DirectAnswerFlow:
    """Create validated direct answer drafts.

    Propagates provider, validation, or routine generation-stop signals.
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
        terminal_error: AIProviderError | DirectAnswerInvalidError | None = None
        terminal_failure_code: str | None = None
        retry_used = False
        outcome: DirectAnswerOutcomeResult | None = None
        failure_code: str | None = None
        stopped = False
        call = await self._recorder.start()

        try:
            with agent_phase(phase="answering", agent_name=self._agent.name):
                try:
                    async with self._runtime_scope_factory() as runtime:
                        for attempt_number in range(1, _MAX_ATTEMPTS + 1):
                            try:
                                draft = await self._generate_draft(
                                    runtime=runtime,
                                    request=request,
                                    previous_answer=previous_answer,
                                    repair_context=repair_context,
                                    previous_output_truncated=previous_output_truncated,
                                    attempt_number=attempt_number,
                                )
                            except _DIRECT_ANSWER_FAILURES as exc:
                                failure = classify_direct_answer_failure(exc)
                                retriable = (
                                    failure.request_retry_disposition
                                    is RequestRetryDisposition.RETRY_IN_REQUEST
                                    and attempt_number < _MAX_ATTEMPTS
                                )
                                if retriable:
                                    repair_context = str(exc)
                                    previous_output_truncated = isinstance(
                                        exc, AIProviderOutputTruncatedError
                                    )
                                    retry_used = True
                                    continue
                                terminal_error = exc
                                terminal_failure_code = failure.code
                                raise
                            retry_used = attempt_number > 1
                            outcome = "answered"
                            return draft
                except _DIRECT_ANSWER_FAILURES as exc:
                    if exc is terminal_error:
                        outcome = "failed"
                        failure_code = terminal_failure_code
                    raise

                raise AssertionError("unreachable: answer loop must return or raise")
        except (asyncio.CancelledError, GeneratorExit, AnswerGenerationStopped):
            stopped = True
            outcome = None
            failure_code = None
            raise
        finally:
            await self._recorder.end(
                call,
                outcome=outcome,
                retry_used=retry_used,
                failure_code=failure_code,
                stopped=stopped,
            )

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
