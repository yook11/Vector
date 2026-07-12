"""Validated direct answer flow."""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from datetime import datetime

from app.agent.answering.audit import (
    DirectAnswerAttemptFailureEvent,
    DirectAnswerAuditRecorder,
    DirectAnswerFailureAttributes,
    DirectAnswerFinalEvent,
    RequestRetryDisposition,
    classify_direct_answer_failure,
)
from app.agent.answering.direct_answer.contract import (
    AnswerGenerationStopped,
    DirectAnswerDraft,
    DirectAnswerGenerator,
    DirectAnswerInvalidError,
)
from app.agent.answering.direct_answer.stream_filter import (
    DirectAnswerVisibleTextFilter,
)
from app.agent.answering.metrics import record_direct_answer_outcome
from app.agent.contract import AnswerDeltaReporter, AnswerGenerationContinuation
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
        audit_recorder: DirectAnswerAuditRecorder | None = None,
        delta_reporter: AnswerDeltaReporter | None = None,
        continuation: AnswerGenerationContinuation | None = None,
    ) -> None:
        self._generator = generator
        self._audit_recorder = audit_recorder
        self._delta_reporter = delta_reporter
        self._continuation = continuation

    async def answer(
        self,
        *,
        question: str,
        as_of: datetime,
        user_intent: str = "",
        user_activity_context: str = "",
        previous_answer: str = "",
    ) -> DirectAnswerDraft:
        """Return a valid direct draft, retrying only blank response defects."""

        ai_model = _generator_attr(self._generator, "model_name")
        prompt_version = _generator_attr(self._generator, "prompt_version")
        previous_error: str | None = None

        for attempt_number in range(1, _MAX_ATTEMPTS + 1):
            try:
                draft = await self._generate_draft(
                    question=question,
                    as_of=as_of,
                    user_intent=user_intent,
                    user_activity_context=user_activity_context,
                    previous_answer=previous_answer,
                    previous_error=previous_error,
                    generation=attempt_number,
                )
            except _DIRECT_ANSWER_FAILURES as exc:
                failure = classify_direct_answer_failure(exc)
                await _record_attempt_failure(
                    audit_recorder=self._audit_recorder,
                    attempt_number=attempt_number,
                    failure=failure,
                    ai_model=ai_model,
                    prompt_version=prompt_version,
                )
                retriable = (
                    failure.request_retry_disposition
                    is RequestRetryDisposition.RETRY_IN_REQUEST
                    and attempt_number < _MAX_ATTEMPTS
                )
                if not retriable:
                    await self._record_failed(
                        attempt_count=attempt_number,
                        retry_used=attempt_number > 1,
                        failure=failure,
                        ai_model=ai_model,
                        prompt_version=prompt_version,
                    )
                    raise
                previous_error = str(exc)
                continue

            await self._record_answered(
                attempt_count=attempt_number,
                retry_used=attempt_number > 1,
                ai_model=ai_model,
                prompt_version=prompt_version,
            )
            return draft

        raise AssertionError("unreachable: answer loop must return or raise")

    async def _generate_draft(
        self,
        *,
        question: str,
        as_of: datetime,
        user_intent: str,
        user_activity_context: str,
        previous_answer: str,
        previous_error: str | None,
        generation: int,
    ) -> DirectAnswerDraft:
        stream: AsyncIterator[str] | None = None
        stream_filter = DirectAnswerVisibleTextFilter()
        raw_fragments: list[str] = []
        try:
            if not await self._should_continue():
                raise AnswerGenerationStopped

            stream = self._generator.stream(
                question=question,
                as_of=as_of,
                user_intent=user_intent,
                user_activity_context=user_activity_context,
                previous_answer=previous_answer,
                previous_error=previous_error,
            )
            async for fragment in stream:
                if not await self._should_continue():
                    raise AnswerGenerationStopped
                raw_fragments.append(fragment)
                visible = stream_filter.append(fragment)
                if visible:
                    await self._report_append(generation=generation, text=visible)

            if not await self._should_continue():
                raise AnswerGenerationStopped
            visible_tail = stream_filter.finish()
            answer = _CITATION_MARKER_RE.sub("", "".join(raw_fragments))
            if not answer.strip():
                raise DirectAnswerInvalidError()
            draft = DirectAnswerDraft(answer=answer)

            if visible_tail:
                await self._report_append(generation=generation, text=visible_tail)
            await self._report_finish(generation=generation)
            return draft
        except BaseException:
            await self._report_abort(generation=generation)
            raise
        finally:
            await _close_stream(stream)

    async def _should_continue(self) -> bool:
        if self._continuation is None:
            return True
        return await self._continuation.should_continue()

    async def _report_append(self, *, generation: int, text: str) -> None:
        if self._delta_reporter is None:
            return
        try:
            await self._delta_reporter.append(generation=generation, text=text)
        except Exception:
            return

    async def _report_finish(self, *, generation: int) -> None:
        if self._delta_reporter is None:
            return
        try:
            await self._delta_reporter.finish(generation=generation)
        except Exception:
            return

    async def _report_abort(self, *, generation: int) -> None:
        if self._delta_reporter is None:
            return
        try:
            await self._delta_reporter.abort(generation=generation)
        except Exception:
            return

    async def _record_answered(
        self,
        *,
        attempt_count: int,
        retry_used: bool,
        ai_model: str | None,
        prompt_version: str | None,
    ) -> None:
        event = DirectAnswerFinalEvent.answered(
            attempt_count=attempt_count,
            retry_used=retry_used,
            ai_model=ai_model,
            prompt_version=prompt_version,
        )
        await _record_final_event(self._audit_recorder, event)
        record_direct_answer_outcome(result="answered", retry_used=retry_used)

    async def _record_failed(
        self,
        *,
        attempt_count: int,
        retry_used: bool,
        failure: DirectAnswerFailureAttributes,
        ai_model: str | None,
        prompt_version: str | None,
    ) -> None:
        event = DirectAnswerFinalEvent.failed(
            attempt_count=attempt_count,
            retry_used=retry_used,
            failure=failure,
            ai_model=ai_model,
            prompt_version=prompt_version,
        )
        await _record_final_event(self._audit_recorder, event)
        record_direct_answer_outcome(result="failed", retry_used=retry_used)


def _generator_attr(generator: DirectAnswerGenerator, name: str) -> str | None:
    value = getattr(generator, name, None)
    return value if isinstance(value, str) else None


async def _record_attempt_failure(
    *,
    audit_recorder: DirectAnswerAuditRecorder | None,
    attempt_number: int,
    failure: DirectAnswerFailureAttributes,
    ai_model: str | None,
    prompt_version: str | None,
) -> None:
    if audit_recorder is None:
        return
    event = DirectAnswerAttemptFailureEvent.from_failure(
        attempt_number=attempt_number,
        failure=failure,
        ai_model=ai_model,
        prompt_version=prompt_version,
    )
    try:
        await audit_recorder.record_attempt_failure(event)
    except Exception:
        return


async def _record_final_event(
    audit_recorder: DirectAnswerAuditRecorder | None,
    event: DirectAnswerFinalEvent,
) -> None:
    if audit_recorder is None:
        return
    try:
        await audit_recorder.record_final_event(event)
    except Exception:
        return


async def _close_stream(stream: AsyncIterator[str] | None) -> None:
    if stream is None:
        return
    close = getattr(stream, "aclose", None)
    if close is None:
        return
    try:
        await close()
    except Exception:
        return
