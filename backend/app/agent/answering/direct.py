"""Direct answer port and draft contract."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from pydantic import BaseModel, ConfigDict

from app.agent.answering.audit import (
    DIRECT_ANSWER_BLANK_RESPONSE,
    DirectAnswerAttemptFailureEvent,
    DirectAnswerAuditRecorder,
    DirectAnswerFailureAttributes,
    DirectAnswerFinalEvent,
    RequestRetryDisposition,
    classify_direct_answer_failure,
)
from app.agent.answering.metrics import record_direct_answer_outcome
from app.agent.contract import NonBlankText
from app.analysis.ai_provider_errors import AIProviderError

__all__ = [
    "DirectAnswerDraft",
    "DirectAnswerer",
    "DirectAnswerGenerator",
    "DirectAnswerInvalidError",
    "DirectAnswerService",
]


class DirectAnswerInvalidError(ValueError):
    """Direct answer response が answer draft として消化できない。"""

    def __init__(self, code: str = DIRECT_ANSWER_BLANK_RESPONSE) -> None:
        self.code = code
        super().__init__(code)


class DirectAnswerDraft(BaseModel):
    """Direct 回答工程 (LLM) の出力 draft。"""

    model_config = ConfigDict(frozen=True)

    answer: NonBlankText


class DirectAnswerGenerator(Protocol):
    """LLM adapter boundary that returns unvalidated direct answer text."""

    async def generate(
        self,
        *,
        question: str,
        as_of: datetime,
        previous_error: str | None = None,
    ) -> str: ...


class DirectAnswerer(Protocol):
    """検索なしで自然に回答する工程。

    失敗時は AIProviderError | DirectAnswerInvalidError を伝播する。
    """

    async def answer(
        self,
        *,
        question: str,
        as_of: datetime,
    ) -> DirectAnswerDraft: ...


_DIRECT_AUDITED_ERRORS = (AIProviderError, DirectAnswerInvalidError)


class DirectAnswerService:
    """Create direct answer drafts.

    Propagates AIProviderError or DirectAnswerInvalidError.
    """

    def __init__(
        self,
        *,
        generator: DirectAnswerGenerator,
        audit_recorder: DirectAnswerAuditRecorder | None = None,
    ) -> None:
        self._generator = generator
        self._audit_recorder = audit_recorder

    async def answer(
        self,
        *,
        question: str,
        as_of: datetime,
    ) -> DirectAnswerDraft:
        """Return a valid direct draft, retrying only blank response defects."""

        ai_model = _generator_attr(self._generator, "model_name")
        prompt_version = _generator_attr(self._generator, "prompt_version")
        try:
            draft = await self._generate_draft(
                question=question,
                as_of=as_of,
                previous_error=None,
            )
        except _DIRECT_AUDITED_ERRORS as exc:
            failure = classify_direct_answer_failure(exc)
            await _record_attempt_failure(
                audit_recorder=self._audit_recorder,
                attempt_number=1,
                failure=failure,
                ai_model=ai_model,
                prompt_version=prompt_version,
            )
            if (
                failure.request_retry_disposition
                is not RequestRetryDisposition.RETRY_IN_REQUEST
            ):
                await self._record_failed(
                    attempt_count=1,
                    retry_used=False,
                    failure=failure,
                    ai_model=ai_model,
                    prompt_version=prompt_version,
                )
                raise
            try:
                draft = await self._generate_draft(
                    question=question,
                    as_of=as_of,
                    previous_error=str(exc),
                )
            except _DIRECT_AUDITED_ERRORS as retry_exc:
                retry_failure = classify_direct_answer_failure(retry_exc)
                await _record_attempt_failure(
                    audit_recorder=self._audit_recorder,
                    attempt_number=2,
                    failure=retry_failure,
                    ai_model=ai_model,
                    prompt_version=prompt_version,
                )
                await self._record_failed(
                    attempt_count=2,
                    retry_used=True,
                    failure=retry_failure,
                    ai_model=ai_model,
                    prompt_version=prompt_version,
                )
                raise
            await self._record_answered(
                attempt_count=2,
                retry_used=True,
                ai_model=ai_model,
                prompt_version=prompt_version,
            )
            return draft

        await self._record_answered(
            attempt_count=1,
            retry_used=False,
            ai_model=ai_model,
            prompt_version=prompt_version,
        )
        return draft

    async def _generate_draft(
        self,
        *,
        question: str,
        as_of: datetime,
        previous_error: str | None,
    ) -> DirectAnswerDraft:
        answer = await self._generator.generate(
            question=question,
            as_of=as_of,
            previous_error=previous_error,
        )
        if not answer.strip():
            raise DirectAnswerInvalidError()
        return DirectAnswerDraft(answer=answer)

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
