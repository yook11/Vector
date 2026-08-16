"""Validated direct answer flow."""

from __future__ import annotations

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
from app.agent.answering.metrics import record_direct_answer_outcome
from app.agent.citation_markers import strip_citation_markers
from app.agent.contract import (
    AnswerDeltaReporter,
    AnswerGenerationContinuation,
)
from app.agent.phase_span import agent_phase
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
    ) -> None:
        self._agent = agent
        self._runtime_scope_factory = runtime_scope_factory
        self._delta = BestEffortAnswerDeltaReporter(delta_reporter)
        self._continuation = continuation

    async def answer(
        self,
        *,
        request: AnsweringRequest,
        previous_answer: str = "",
    ) -> DirectAnswerDraft:
        """Return a valid direct draft, retrying only blank response defects."""

        with agent_phase(phase="answering", agent_name=self._agent.name):
            async with self._runtime_scope_factory() as runtime:
                repair_context: str | None = None
                previous_output_truncated = False

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
                        if not retriable:
                            record_direct_answer_outcome(
                                result="failed",
                                retry_used=attempt_number > 1,
                                failure_code=failure.code,
                            )
                            raise
                        repair_context = str(exc)
                        previous_output_truncated = isinstance(
                            exc, AIProviderOutputTruncatedError
                        )
                        continue

                    record_direct_answer_outcome(
                        result="answered",
                        retry_used=attempt_number > 1,
                    )
                    return draft

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
