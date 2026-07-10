"""Validated evidence-grounded answer flow."""

from __future__ import annotations

from datetime import datetime

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
from app.agent.answering.evidence_answer.contract import (
    EvidenceAnswerDraft,
    EvidenceAnswerDraftGenerationInvalidError,
    EvidenceAnswerDraftGenerator,
    EvidenceAnswerDraftInvalidError,
)
from app.agent.answering.evidence_answer.evidence import AnswerEvidenceItem
from app.agent.answering.evidence_answer.validation import (
    finalize_evidence_answer_draft,
)
from app.agent.answering.metrics import record_answer_synthesis_outcome
from app.analysis.ai_provider_errors import AIProviderError

__all__ = ["EvidenceAnswerFlow"]

_FALLBACK_ANSWER = (
    "回答を生成できませんでした。根拠の不足または応答形式の不備により、"
    "参考回答を安全に構築できませんでした。"
)
_FALLBACK_MISSING_ASPECT = "回答生成に必要な根拠または応答形式が不足しました"
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
    ) -> None:
        self._generator = generator
        self._audit_recorder = audit_recorder

    async def answer(
        self,
        *,
        question: str,
        evidence: list[AnswerEvidenceItem],
        as_of: datetime,
        target_time_window: str | None,
        user_intent: str = "",
        prior_coverage: str = "",
        user_activity_context: str = "",
    ) -> EvidenceAnswerDraft:
        """Return a valid draft, retrying audited response-boundary failures."""

        ai_model = _generator_attr(self._generator, "model_name")
        prompt_version = _generator_attr(self._generator, "prompt_version")
        defect_count = 0

        try:
            draft, defects = await self._generate_strict_draft(
                question=question,
                evidence=evidence,
                as_of=as_of,
                target_time_window=target_time_window,
                user_intent=user_intent,
                prior_coverage=prior_coverage,
                user_activity_context=user_activity_context,
                previous_error=None,
                attempt_number=1,
                ai_model=ai_model,
                prompt_version=prompt_version,
            )
        except _EVIDENCE_ANSWER_AUDITED_ERRORS as exc:
            failure = classify_answer_synthesis_failure(exc)
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
                return await self._fallback_with_audit(
                    attempt_count=1,
                    retry_used=False,
                    failure=failure,
                    evidence_count=len(evidence),
                    defect_count=defect_count,
                    ai_model=ai_model,
                    prompt_version=prompt_version,
                )
            try:
                draft, defects = await self._generate_strict_draft(
                    question=question,
                    evidence=evidence,
                    as_of=as_of,
                    target_time_window=target_time_window,
                    user_intent=user_intent,
                    prior_coverage=prior_coverage,
                    user_activity_context=user_activity_context,
                    previous_error=str(exc),
                    attempt_number=2,
                    ai_model=ai_model,
                    prompt_version=prompt_version,
                )
            except _EVIDENCE_ANSWER_AUDITED_ERRORS as retry_exc:
                retry_failure = classify_answer_synthesis_failure(retry_exc)
                await _record_attempt_failure(
                    audit_recorder=self._audit_recorder,
                    attempt_number=2,
                    failure=retry_failure,
                    ai_model=ai_model,
                    prompt_version=prompt_version,
                )
                return await self._fallback_with_audit(
                    attempt_count=2,
                    retry_used=True,
                    failure=retry_failure,
                    evidence_count=len(evidence),
                    defect_count=defect_count,
                    ai_model=ai_model,
                    prompt_version=prompt_version,
                )
            defect_count += len(defects)
            await self._record_synthesized(
                draft=draft,
                attempt_count=2,
                retry_used=True,
                evidence_count=len(evidence),
                defect_count=defect_count,
                ai_model=ai_model,
                prompt_version=prompt_version,
            )
            return draft

        defect_count += len(defects)
        await self._record_synthesized(
            draft=draft,
            attempt_count=1,
            retry_used=False,
            evidence_count=len(evidence),
            defect_count=defect_count,
            ai_model=ai_model,
            prompt_version=prompt_version,
        )
        return draft

    async def _generate_strict_draft(
        self,
        *,
        question: str,
        evidence: list[AnswerEvidenceItem],
        as_of: datetime,
        target_time_window: str | None,
        user_intent: str,
        prior_coverage: str,
        user_activity_context: str,
        previous_error: str | None,
        attempt_number: int,
        ai_model: str | None,
        prompt_version: str | None,
    ) -> tuple[EvidenceAnswerDraft, list[str]]:
        raw = await self._generator.generate(
            question=question,
            evidence=evidence,
            as_of=as_of,
            target_time_window=target_time_window,
            user_intent=user_intent,
            prior_coverage=prior_coverage,
            user_activity_context=user_activity_context,
            previous_error=previous_error,
        )
        draft, defects = finalize_evidence_answer_draft(raw, evidence=evidence)
        for defect in defects:
            await _record_defect(
                audit_recorder=self._audit_recorder,
                attempt_number=attempt_number,
                defect_code=defect,
                ai_model=ai_model,
                prompt_version=prompt_version,
            )
        return draft, defects

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
        attempt_count: int,
        retry_used: bool,
        failure: AnswerSynthesisFailureAttributes,
        evidence_count: int,
        defect_count: int,
        ai_model: str | None,
        prompt_version: str | None,
    ) -> EvidenceAnswerDraft:
        fallback = EvidenceAnswerDraft(
            sufficiency="insufficient",
            answer=_FALLBACK_ANSWER,
            cited_refs=[],
            missing_aspects=[_FALLBACK_MISSING_ASPECT],
        )
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
