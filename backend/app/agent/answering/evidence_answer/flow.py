"""Validated evidence-grounded answer flow."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import logfire
from opentelemetry.trace import StatusCode
from pydantic import ValidationError

from app.agent.agent import Agent
from app.agent.answering.contract import AnsweringRequest
from app.agent.answering.evidence_answer.contract import (
    EvidenceAnswerDraft,
    EvidenceAnswerDraftInvalidError,
    EvidenceAnswerInput,
    EvidenceAnswerOutcome,
    EvidenceAnswerUnavailable,
)
from app.agent.answering.evidence_answer.evidence import AnswerEvidenceItem
from app.agent.answering.evidence_answer.validation import (
    finalize_evidence_answer_draft,
)
from app.agent.answering.failure import (
    AnswerSynthesisFailureAttributes,
    RequestRetryDisposition,
    classify_answer_synthesis_failure,
)
from app.agent.answering.live_delivery import (
    BestEffortAnswerDeltaReporter,
    close_answer_stream,
    ensure_answer_generation_continues,
)
from app.agent.answering.live_draft import LiveAnswerDraftSession
from app.agent.answering.metrics import record_answer_synthesis_outcome
from app.agent.contract import (
    AnswerDeltaReporter,
    AnswerGenerationContinuation,
    AnswerGenerationStopped,
)
from app.agent.planning.contract import TargetTimeWindow
from app.agent.runtime.contract import (
    AgentTextStream,
    StreamingAgentRuntime,
    StreamingAgentRuntimeScopeFactory,
)
from app.analysis.ai_provider_errors import (
    AIProviderError,
    AIProviderOutputTruncatedError,
)

__all__ = ["EvidenceAnswerFlow"]

_MAX_ATTEMPTS = 2
_PHASE_SPAN_NAME = "agent_phase"
# ValidationError(pydantic)は、plain text化後のfinalize_evidence_answer_draft()が
# 空白判定を先に行うため通常経路では到達しない。classify_answer_synthesis_failure()側の
# 分類も維持されており、EvidenceAnswerDraftの構築自体が将来pydantic validationで
# 失敗しうる防御的なゲートとして残すため、caught setからは外さない。
_EVIDENCE_ANSWER_CLASSIFIED_ERRORS = (
    AIProviderError,
    EvidenceAnswerDraftInvalidError,
    ValidationError,
)


class EvidenceAnswerFlow:
    """Create strict evidence answer drafts from plain text LLM output."""

    def __init__(
        self,
        *,
        agent: Agent[EvidenceAnswerInput, EvidenceAnswerDraft],
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
        evidence: list[AnswerEvidenceItem],
        target_time_window: TargetTimeWindow | None,
        review_missing: tuple[str, ...],
    ) -> EvidenceAnswerOutcome:
        """Return a valid draft or an unavailable outcome.

        Retries classified response-boundary failures within the attempt budget.
        """

        with _evidence_answer_phase(self._agent.name):
            async with self._runtime_scope_factory() as runtime:
                previous_error: str | None = None
                previous_output_truncated = False

                for attempt_number in range(1, _MAX_ATTEMPTS + 1):
                    try:
                        draft = await self._generate_strict_draft(
                            runtime=runtime,
                            request=request,
                            evidence=evidence,
                            target_time_window=target_time_window,
                            previous_error=previous_error,
                            previous_output_truncated=previous_output_truncated,
                            review_missing=review_missing,
                            attempt_number=attempt_number,
                        )
                    except _EVIDENCE_ANSWER_CLASSIFIED_ERRORS as exc:
                        failure = classify_answer_synthesis_failure(exc)
                        retriable = (
                            failure.request_retry_disposition
                            is RequestRetryDisposition.RETRY_IN_REQUEST
                            and attempt_number < _MAX_ATTEMPTS
                        )
                        if not retriable:
                            return await self._fallback(
                                generation=attempt_number + 1,
                                retry_used=attempt_number > 1,
                                failure=failure,
                            )
                        await self._start_revision(generation=attempt_number + 1)
                        previous_error = str(exc)
                        previous_output_truncated = isinstance(
                            exc, AIProviderOutputTruncatedError
                        )
                        continue

                    record_answer_synthesis_outcome(
                        result="synthesized",
                        retry_used=attempt_number > 1,
                        fallback_used=False,
                    )
                    return draft

        raise AssertionError("unreachable: answer loop must return or raise")

    async def _generate_strict_draft(
        self,
        *,
        runtime: StreamingAgentRuntime,
        request: AnsweringRequest,
        evidence: list[AnswerEvidenceItem],
        target_time_window: TargetTimeWindow | None,
        previous_error: str | None,
        previous_output_truncated: bool,
        review_missing: tuple[str, ...],
        attempt_number: int,
    ) -> EvidenceAnswerDraft:
        stream: AgentTextStream | None = None
        raw_fragments: list[str] = []
        try:
            async with LiveAnswerDraftSession(
                generation=attempt_number,
                delta_reporter=self._delta,
            ) as live_draft:
                await ensure_answer_generation_continues(self._continuation)

                stream = runtime.invoke_stream(
                    self._agent,
                    EvidenceAnswerInput(
                        request=request,
                        evidence=tuple(evidence),
                        target_time_window=target_time_window,
                        previous_error=previous_error,
                        previous_output_truncated=previous_output_truncated,
                        review_missing=review_missing,
                    ),
                    attempt_number=attempt_number,
                )
                async for fragment in stream:
                    await ensure_answer_generation_continues(self._continuation)
                    raw_fragments.append(fragment)
                    await live_draft.append(fragment)

                await ensure_answer_generation_continues(self._continuation)
                answer = "".join(raw_fragments)
                draft = finalize_evidence_answer_draft(answer, evidence=evidence)

                await live_draft.commit()
                return draft
        finally:
            await close_answer_stream(stream)

    async def _start_revision(self, *, generation: int) -> None:
        await ensure_answer_generation_continues(self._continuation)
        await self._delta.reset(generation=generation)

    async def _fallback(
        self,
        *,
        generation: int,
        retry_used: bool,
        failure: AnswerSynthesisFailureAttributes,
    ) -> EvidenceAnswerUnavailable:
        await self._start_revision(generation=generation)
        await self._delta.finish(generation=generation)
        record_answer_synthesis_outcome(
            result="fallback",
            retry_used=retry_used,
            fallback_used=True,
            failure_code=failure.code,
        )
        return EvidenceAnswerUnavailable(failure_code=failure.code)


@contextmanager
def _evidence_answer_phase(agent_name: str) -> Iterator[None]:
    stopped: AnswerGenerationStopped | None = None
    with logfire.span(
        _PHASE_SPAN_NAME,
        phase="evidence_answer",
        agent_name=agent_name,
    ) as span:
        try:
            yield
        except AnswerGenerationStopped as exc:
            stopped = exc
        except BaseException:
            _record_unclassified_phase_error(span)
            raise
    if stopped is not None:
        raise stopped


def _record_unclassified_phase_error(span: Any) -> None:
    span.set_status(StatusCode.ERROR, "unclassified agent phase error")
