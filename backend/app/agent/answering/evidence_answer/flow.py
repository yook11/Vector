"""Validated evidence-grounded answer flow."""

from __future__ import annotations

from collections.abc import AsyncIterator

from pydantic import ValidationError

from app.agent.answering.audit import (
    AnswerSynthesisAttemptFailureEvent,
    AnswerSynthesisAuditRecorder,
    AnswerSynthesisDefectEvent,
    AnswerSynthesisFailureAttributes,
    AnswerSynthesisFinalEvent,
    RequestRetryDisposition,
    classify_answer_synthesis_failure,
)
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
_EVIDENCE_ANSWER_AUDITED_ERRORS = (
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
        audit_recorder: AnswerSynthesisAuditRecorder | None = None,
        delta_reporter: AnswerDeltaReporter | None = None,
        continuation: AnswerGenerationContinuation | None = None,
    ) -> None:
        self._generator = generator
        self._audit_recorder = audit_recorder
        self._delta = BestEffortAnswerDeltaReporter(delta_reporter)
        self._continuation = continuation

    async def answer(
        self,
        *,
        request: AnsweringRequest,
        evidence: list[AnswerEvidenceItem],
        target_time_window: str | None,
    ) -> EvidenceAnswerDraft:
        """Return a valid draft, retrying audited response-boundary failures."""

        ai_model = _generator_attr(self._generator, "model_name")
        prompt_version = _generator_attr(self._generator, "prompt_version")
        previous_error: str | None = None
        defect_count = 0

        for attempt_number in range(1, _MAX_ATTEMPTS + 1):
            try:
                draft, defects = await self._generate_strict_draft(
                    request=request,
                    evidence=evidence,
                    target_time_window=target_time_window,
                    previous_error=previous_error,
                    attempt_number=attempt_number,
                    generation=attempt_number,
                    ai_model=ai_model,
                    prompt_version=prompt_version,
                )
            except _EVIDENCE_ANSWER_AUDITED_ERRORS as exc:
                failure = classify_answer_synthesis_failure(exc)
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
                    return await self._fallback_with_audit(
                        generation=attempt_number + 1,
                        attempt_count=attempt_number,
                        retry_used=attempt_number > 1,
                        failure=failure,
                        evidence_count=len(evidence),
                        defect_count=defect_count,
                        ai_model=ai_model,
                        prompt_version=prompt_version,
                    )
                await self._start_revision(generation=attempt_number + 1)
                previous_error = str(exc)
                continue

            defect_count += len(defects)
            await self._record_synthesized(
                draft=draft,
                attempt_count=attempt_number,
                retry_used=attempt_number > 1,
                evidence_count=len(evidence),
                defect_count=defect_count,
                ai_model=ai_model,
                prompt_version=prompt_version,
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
        attempt_number: int,
        generation: int,
        ai_model: str | None,
        prompt_version: str | None,
    ) -> tuple[EvidenceAnswerDraft, list[str]]:
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
                draft, defects = finalize_evidence_answer_draft(raw, evidence=evidence)
                for defect in defects:
                    await _record_defect(
                        audit_recorder=self._audit_recorder,
                        attempt_number=attempt_number,
                        defect_code=defect,
                        ai_model=ai_model,
                        prompt_version=prompt_version,
                    )

                await live_draft.commit()
                return draft, defects
        finally:
            await close_answer_stream(stream)

    async def _start_revision(self, *, generation: int) -> None:
        await ensure_answer_generation_continues(self._continuation)
        await self._delta.reset(generation=generation)

    async def _record_synthesized(
        self,
        *,
        draft: EvidenceAnswerDraft,
        attempt_count: int,
        retry_used: bool,
        evidence_count: int,
        defect_count: int,
        ai_model: str | None,
        prompt_version: str | None,
    ) -> None:
        event = AnswerSynthesisFinalEvent.synthesized(
            attempt_count=attempt_count,
            retry_used=retry_used,
            status=draft.sufficiency,
            evidence_count=evidence_count,
            cited_ref_count=len(draft.cited_refs),
            missing_aspect_count=len(draft.missing_aspects),
            defect_count=defect_count,
            ai_model=ai_model,
            prompt_version=prompt_version,
        )
        await _record_final_event(self._audit_recorder, event)
        record_answer_synthesis_outcome(
            result="synthesized",
            retry_used=retry_used,
            status=draft.sufficiency,
            fallback_used=False,
        )

    async def _fallback_with_audit(
        self,
        *,
        generation: int,
        attempt_count: int,
        retry_used: bool,
        failure: AnswerSynthesisFailureAttributes,
        evidence_count: int,
        defect_count: int,
        ai_model: str | None,
        prompt_version: str | None,
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
        event = AnswerSynthesisFinalEvent.fallback(
            attempt_count=attempt_count,
            retry_used=retry_used,
            draft_status=fallback.sufficiency,
            evidence_count=evidence_count,
            cited_ref_count=len(fallback.cited_refs),
            missing_aspect_count=len(fallback.missing_aspects),
            defect_count=defect_count,
            failure=failure,
            ai_model=ai_model,
            prompt_version=prompt_version,
        )
        await _record_final_event(self._audit_recorder, event)
        record_answer_synthesis_outcome(
            result="fallback",
            retry_used=retry_used,
            status=fallback.sufficiency,
            fallback_used=True,
        )
        return fallback


def _generator_attr(generator: EvidenceAnswerDraftGenerator, name: str) -> str | None:
    value = getattr(generator, name, None)
    return value if isinstance(value, str) else None


async def _record_attempt_failure(
    *,
    audit_recorder: AnswerSynthesisAuditRecorder | None,
    attempt_number: int,
    failure: AnswerSynthesisFailureAttributes,
    ai_model: str | None,
    prompt_version: str | None,
) -> None:
    if audit_recorder is None:
        return
    event = AnswerSynthesisAttemptFailureEvent.from_failure(
        attempt_number=attempt_number,
        failure=failure,
        ai_model=ai_model,
        prompt_version=prompt_version,
    )
    try:
        await audit_recorder.record_attempt_failure(event)
    except Exception:
        return


async def _record_defect(
    *,
    audit_recorder: AnswerSynthesisAuditRecorder | None,
    attempt_number: int,
    defect_code: str,
    ai_model: str | None,
    prompt_version: str | None,
) -> None:
    if audit_recorder is None:
        return
    event = AnswerSynthesisDefectEvent(
        attempt_number=attempt_number,
        defect_code=defect_code,
        ai_model=ai_model,
        prompt_version=prompt_version,
    )
    try:
        await audit_recorder.record_defect(event)
    except Exception:
        return


async def _record_final_event(
    audit_recorder: AnswerSynthesisAuditRecorder | None,
    event: AnswerSynthesisFinalEvent,
) -> None:
    if audit_recorder is None:
        return
    try:
        await audit_recorder.record_final_event(event)
    except Exception:
        return
