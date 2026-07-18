"""Validated evidence-grounded answer flow."""

from __future__ import annotations

from collections.abc import AsyncIterator

from pydantic import ValidationError

from app.agent.answering.contract import AnsweringRequest
from app.agent.answering.evidence_answer.contract import (
    EvidenceAnswerDraft,
    EvidenceAnswerDraftGenerationInvalidError,
    EvidenceAnswerDraftGenerator,
    EvidenceAnswerDraftInvalidError,
)
from app.agent.answering.evidence_answer.evidence import AnswerEvidenceItem
from app.agent.answering.evidence_answer.final_json import (
    parse_evidence_answer_final_json,
)
from app.agent.answering.evidence_answer.json_answer_extractor import (
    IncrementalJsonAnswerExtractor,
)
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
)
from app.analysis.ai_provider_errors import AIProviderError

__all__ = ["EvidenceAnswerFlow"]

_FALLBACK_ANSWER = (
    "回答を生成できませんでした。根拠の不足または応答形式の不備により、"
    "参考回答を安全に構築できませんでした。"
)
_FALLBACK_MISSING_ASPECT = "回答生成に必要な根拠または応答形式が不足しました"
_MAX_ATTEMPTS = 2
_EVIDENCE_ANSWER_CLASSIFIED_ERRORS = (
    AIProviderError,
    EvidenceAnswerDraftGenerationInvalidError,
    EvidenceAnswerDraftInvalidError,
    ValidationError,
)


class EvidenceAnswerFlow:
    """Create strict evidence answer drafts from lenient LLM drafts."""

    def __init__(
        self,
        *,
        generator: EvidenceAnswerDraftGenerator,
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
        evidence: list[AnswerEvidenceItem],
        target_time_window: str | None,
    ) -> EvidenceAnswerDraft:
        """Return a valid draft, retrying classified response-boundary failures."""

        previous_error: str | None = None

        for attempt_number in range(1, _MAX_ATTEMPTS + 1):
            try:
                draft = await self._generate_strict_draft(
                    request=request,
                    evidence=evidence,
                    target_time_window=target_time_window,
                    previous_error=previous_error,
                    generation=attempt_number,
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
                continue

            synthesized_status = (
                "insufficient"
                if draft.unfulfilled_requirement_ids
                else draft.sufficiency
            )
            record_answer_synthesis_outcome(
                result="synthesized",
                retry_used=attempt_number > 1,
                status=synthesized_status,
                fallback_used=False,
            )
            return draft

        raise AssertionError("unreachable: answer loop must return or raise")

    async def _generate_strict_draft(
        self,
        *,
        request: AnsweringRequest,
        evidence: list[AnswerEvidenceItem],
        target_time_window: str | None,
        previous_error: str | None,
        generation: int,
    ) -> EvidenceAnswerDraft:
        stream: AsyncIterator[str] | None = None
        extractor = IncrementalJsonAnswerExtractor()
        raw_fragments: list[str] = []
        try:
            async with LiveAnswerDraftSession(
                generation=generation,
                delta_reporter=self._delta,
            ) as live_draft:
                await ensure_answer_generation_continues(self._continuation)

                stream = self._generator.stream(
                    request=request,
                    evidence=evidence,
                    target_time_window=target_time_window,
                    previous_error=previous_error,
                )
                async for raw_fragment in stream:
                    await ensure_answer_generation_continues(self._continuation)
                    raw_fragments.append(raw_fragment)
                    decoded = extractor.append(raw_fragment)
                    if decoded:
                        await live_draft.append(decoded)

                await ensure_answer_generation_continues(self._continuation)
                extractor.finish()

                raw = parse_evidence_answer_final_json("".join(raw_fragments))
                requirement_ids = [
                    requirement.requirement_id
                    for requirements in (
                        request.context.content_requirements,
                        request.context.response_requirements,
                    )
                    for requirement in requirements
                ]
                draft, _defects = finalize_evidence_answer_draft(
                    raw,
                    evidence=evidence,
                    requirement_ids=requirement_ids,
                )

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
    ) -> EvidenceAnswerDraft:
        await self._start_revision(generation=generation)
        fallback = EvidenceAnswerDraft(
            sufficiency="insufficient",
            answer=_FALLBACK_ANSWER,
            cited_refs=[],
            missing_aspects=[_FALLBACK_MISSING_ASPECT],
        )
        await self._delta.append(generation=generation, text=fallback.answer)
        await self._delta.finish(generation=generation)
        record_answer_synthesis_outcome(
            result="fallback",
            retry_used=retry_used,
            status=fallback.sufficiency,
            fallback_used=True,
            failure_code=failure.code,
        )
        return fallback
