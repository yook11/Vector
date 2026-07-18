"""Validated direct answer flow."""

from __future__ import annotations

import re
from collections.abc import AsyncIterator

from app.agent.answering.contract import AnsweringRequest
from app.agent.answering.direct_answer.contract import (
    DirectAnswerDraft,
    DirectAnswerGenerator,
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
from app.agent.contract import (
    AnswerDeltaReporter,
    AnswerGenerationContinuation,
)
from app.analysis.ai_provider_errors import AIProviderError

__all__ = ["DirectAnswerFlow"]

_DIRECT_ANSWER_FAILURES = (AIProviderError, DirectAnswerInvalidError)
_MAX_ATTEMPTS = 2
_CITATION_MARKER_RE = re.compile(r"\[\[[0-9]+\]\]")


class DirectAnswerFlow:
    """Create validated direct answer drafts.

    Propagates provider, validation, or routine generation-stop signals.
    """

    def __init__(
        self,
        *,
        generator: DirectAnswerGenerator,
        delta_reporter: AnswerDeltaReporter | None = None,
        continuation: AnswerGenerationContinuation | None = None,
    ) -> None:
        self._generator = generator
        self._delta = BestEffortAnswerDeltaReporter(delta_reporter)
        self._continuation = continuation

    async def answer(
        self,
        *,
        request: AnsweringRequest,
        previous_answer: str = "",
    ) -> DirectAnswerDraft:
        """Return a valid direct draft, retrying only blank response defects."""

        previous_error: str | None = None

        for attempt_number in range(1, _MAX_ATTEMPTS + 1):
            try:
                draft = await self._generate_draft(
                    request=request,
                    previous_answer=previous_answer,
                    previous_error=previous_error,
                    generation=attempt_number,
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
                previous_error = str(exc)
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
        request: AnsweringRequest,
        previous_answer: str,
        previous_error: str | None,
        generation: int,
    ) -> DirectAnswerDraft:
        stream: AsyncIterator[str] | None = None
        raw_fragments: list[str] = []
        try:
            async with LiveAnswerDraftSession(
                generation=generation,
                delta_reporter=self._delta,
            ) as live_draft:
                await ensure_answer_generation_continues(self._continuation)

                stream = self._generator.stream(
                    request=request,
                    previous_answer=previous_answer,
                    previous_error=previous_error,
                )
                async for fragment in stream:
                    await ensure_answer_generation_continues(self._continuation)
                    raw_fragments.append(fragment)
                    await live_draft.append(fragment)

                await ensure_answer_generation_continues(self._continuation)
                answer = _CITATION_MARKER_RE.sub("", "".join(raw_fragments))
                if not answer.strip():
                    raise DirectAnswerInvalidError()
                draft = DirectAnswerDraft(answer=answer)

                await live_draft.commit()
                return draft
        finally:
            await close_answer_stream(stream)
