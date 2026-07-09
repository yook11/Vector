"""Answer synthesis port and draft contract."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Literal, Protocol, Self

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from app.agent.answering.audit import (
    AnswerSynthesisAttemptFailureEvent,
    AnswerSynthesisAuditRecorder,
    AnswerSynthesisDefectEvent,
    AnswerSynthesisFailureAttributes,
    AnswerSynthesisFinalEvent,
    RequestRetryDisposition,
    classify_answer_synthesis_failure,
)
from app.agent.answering.evidence import AnswerEvidenceItem
from app.agent.answering.metrics import record_answer_synthesis_outcome
from app.agent.contract import NonBlankText
from app.analysis.ai_provider_errors import AIProviderError

__all__ = [
    "AnswerDraft",
    "AnswerDraftGenerationInvalidError",
    "AnswerDraftInvalidError",
    "AnswerSufficiency",
    "AnswerSynthesisService",
    "EvidenceAnswerSynthesizer",
    "EvidenceAnswerDraftGenerator",
    "RawAnswerDraft",
]

AnswerSufficiency = Literal["answered", "insufficient"]

_FALLBACK_ANSWER = (
    "回答を生成できませんでした。根拠の不足または応答形式の不備により、"
    "参考回答を安全に構築できませんでした。"
)
_FALLBACK_MISSING_ASPECT = "回答生成に必要な根拠または応答形式が不足しました"
_COMPLETED_MISSING_ASPECT = "回答に必要な追加根拠が不足しています"
_DEFECT_MISSING_COMPLETED = "missing_aspects_completed"
_DEFECT_BLANK_CITED_REFS_REMOVED = "blank_cited_refs_removed"
_DEFECT_DUPLICATE_CITED_REFS_REMOVED = "duplicate_cited_refs_removed"
_DEFECT_NON_STRING_CITED_REFS_REMOVED = "non_string_cited_refs_removed"
_DEFECT_CITED_REFS_RECOMPUTED_FROM_MARKERS = "cited_refs_recomputed_from_markers"
_DEFECT_BLANK_MISSING_ASPECTS_REMOVED = "blank_missing_aspects_removed"
_DEFECT_DUPLICATE_MISSING_ASPECTS_REMOVED = "duplicate_missing_aspects_removed"
_DEFECT_NON_STRING_MISSING_ASPECTS_REMOVED = "non_string_missing_aspects_removed"
_CITATION_MARKER_RE = re.compile(r"\[\[([0-9]+)\]\]")


class AnswerDraftGenerationInvalidError(ValueError):
    """LLM response envelope が raw draft として消化できない。"""

    def __init__(self, defect_code: str) -> None:
        self.defect_code = defect_code
        super().__init__(defect_code)


class AnswerDraft(BaseModel):
    """Evidence 回答工程 (LLM) の出力 draft。"""

    model_config = ConfigDict(frozen=True)

    sufficiency: AnswerSufficiency
    answer: NonBlankText
    cited_refs: list[str] = Field(default_factory=list)
    missing_aspects: list[NonBlankText] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_sufficiency_contract(self) -> Self:
        if self.sufficiency == "answered":
            if self.missing_aspects:
                raise ValueError("answered draft cannot include missing aspects")
            if not self.cited_refs:
                raise ValueError("answered draft requires at least one citation")
        if self.sufficiency == "insufficient" and not self.missing_aspects:
            raise ValueError("insufficient draft must include missing aspects")
        return self


class RawAnswerDraft(BaseModel):
    """LLM adapter boundary の lenient draft。"""

    model_config = ConfigDict(frozen=True)

    sufficiency: object | None = None
    answer: object | None = None
    cited_refs: list[object] = Field(default_factory=list)
    missing_aspects: list[object] = Field(default_factory=list)


class EvidenceAnswerDraftGenerator(Protocol):
    """LLM adapter boundary that returns lenient evidence answer drafts."""

    async def generate(
        self,
        *,
        question: str,
        evidence: list[AnswerEvidenceItem],
        as_of: datetime,
        target_time_window: str | None,
        user_intent: str = "",
        prior_coverage: str = "",
        user_activity_context: str = "",
        previous_error: str | None = None,
    ) -> RawAnswerDraft: ...


class EvidenceAnswerSynthesizer(Protocol):
    """evidence に接地し、answer marker と cited_refs が整合した draft を返す。"""

    async def synthesize(
        self,
        *,
        question: str,
        evidence: list[AnswerEvidenceItem],
        as_of: datetime,
        target_time_window: str | None,
        user_intent: str = "",
        prior_coverage: str = "",
        user_activity_context: str = "",
    ) -> AnswerDraft: ...


class AnswerDraftInvalidError(Exception):
    """draft が evidence への接地契約を破ったことを表す typed error。"""


_SYNTHESIS_AUDITED_ERRORS = (
    AIProviderError,
    AnswerDraftGenerationInvalidError,
    AnswerDraftInvalidError,
    ValidationError,
)


class AnswerSynthesisService:
    """Create strict answer drafts from lenient LLM drafts."""

    def __init__(
        self,
        *,
        generator: EvidenceAnswerDraftGenerator,
        audit_recorder: AnswerSynthesisAuditRecorder | None = None,
    ) -> None:
        self._generator = generator
        self._audit_recorder = audit_recorder

    async def synthesize(
        self,
        *,
        question: str,
        evidence: list[AnswerEvidenceItem],
        as_of: datetime,
        target_time_window: str | None,
        user_intent: str = "",
        prior_coverage: str = "",
        user_activity_context: str = "",
    ) -> AnswerDraft:
        """Return a valid draft, retrying only audited response-boundary failures."""

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
        except _SYNTHESIS_AUDITED_ERRORS as exc:
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
            except _SYNTHESIS_AUDITED_ERRORS as retry_exc:
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
    ) -> tuple[AnswerDraft, list[str]]:
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
        draft, defects = _draft_from_raw(raw, evidence=evidence)
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
        draft: AnswerDraft,
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
    ) -> AnswerDraft:
        fallback = AnswerDraft(
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


def _draft_from_raw(
    raw: RawAnswerDraft,
    *,
    evidence: list[AnswerEvidenceItem],
) -> tuple[AnswerDraft, list[str]]:
    defects: list[str] = []
    sufficiency = _sufficiency_from_raw(raw.sufficiency)
    cited_refs, cited_ref_defects = _clean_string_list(
        raw.cited_refs,
        blank_defect=_DEFECT_BLANK_CITED_REFS_REMOVED,
        duplicate_defect=_DEFECT_DUPLICATE_CITED_REFS_REMOVED,
        non_string_defect=_DEFECT_NON_STRING_CITED_REFS_REMOVED,
    )
    missing_aspects, missing_defects = _clean_string_list(
        raw.missing_aspects,
        blank_defect=_DEFECT_BLANK_MISSING_ASPECTS_REMOVED,
        duplicate_defect=_DEFECT_DUPLICATE_MISSING_ASPECTS_REMOVED,
        non_string_defect=_DEFECT_NON_STRING_MISSING_ASPECTS_REMOVED,
    )
    defects.extend(cited_ref_defects)
    defects.extend(missing_defects)

    if isinstance(raw.answer, str):
        marker_refs = _citation_refs_from_answer(raw.answer)
        if sufficiency == "answered" and not marker_refs:
            raise AnswerDraftInvalidError(
                "answered answer requires at least one citation marker"
            )
        if cited_refs != marker_refs:
            cited_refs = marker_refs
            defects.append(_DEFECT_CITED_REFS_RECOMPUTED_FROM_MARKERS)

    if sufficiency == "insufficient" and not missing_aspects:
        missing_aspects = [_COMPLETED_MISSING_ASPECT]
        defects.append(_DEFECT_MISSING_COMPLETED)

    draft = AnswerDraft(
        sufficiency=sufficiency,
        answer=raw.answer,
        cited_refs=cited_refs,
        missing_aspects=missing_aspects,
    )
    _validate_draft_citations(evidence=evidence, draft=draft)
    return draft, defects


def _citation_refs_from_answer(answer: str) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for match in _CITATION_MARKER_RE.finditer(answer):
        ref = match.group(1)
        if ref in seen:
            continue
        result.append(ref)
        seen.add(ref)
    return result


def _sufficiency_from_raw(value: object | None) -> AnswerSufficiency:
    if value in ("answered", "insufficient"):
        return value
    raise AnswerDraftInvalidError("unknown answer sufficiency")


def _clean_string_list(
    values: list[object],
    *,
    blank_defect: str,
    duplicate_defect: str,
    non_string_defect: str,
) -> tuple[list[str], list[str]]:
    result: list[str] = []
    seen: set[str] = set()
    defect_set: set[str] = set()
    for value in values:
        if not isinstance(value, str):
            defect_set.add(non_string_defect)
            continue
        stripped = value.strip()
        if not stripped:
            defect_set.add(blank_defect)
            continue
        if stripped in seen:
            defect_set.add(duplicate_defect)
            continue
        result.append(stripped)
        seen.add(stripped)
    return result, list(defect_set)


def _validate_draft_citations(
    *,
    evidence: list[AnswerEvidenceItem],
    draft: AnswerDraft,
) -> None:
    existing_refs = {item.source.source_ref for item in evidence}
    unknown_refs = [ref for ref in draft.cited_refs if ref not in existing_refs]
    if unknown_refs:
        unknown_ref = unknown_refs[0]
        raise AnswerDraftInvalidError(
            "answer 本文の citation marker "
            f"[[{unknown_ref}]] は evidence に存在しません"
        )


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
