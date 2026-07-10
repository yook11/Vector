"""Deterministic evidence answer draft finalization."""

from __future__ import annotations

import re

from app.agent.answering.evidence_answer.contract import (
    EvidenceAnswerDraft,
    EvidenceAnswerDraftInvalidError,
    EvidenceAnswerSufficiency,
    RawEvidenceAnswerDraft,
)
from app.agent.answering.evidence_answer.evidence import AnswerEvidenceItem

__all__ = ["finalize_evidence_answer_draft"]

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


def finalize_evidence_answer_draft(
    raw: RawEvidenceAnswerDraft,
    *,
    evidence: list[AnswerEvidenceItem],
) -> tuple[EvidenceAnswerDraft, list[str]]:
    """Apply deterministic repairs and return a strict evidence answer draft."""

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
            raise EvidenceAnswerDraftInvalidError(
                "answered answer requires at least one citation marker"
            )
        if cited_refs != marker_refs:
            cited_refs = marker_refs
            defects.append(_DEFECT_CITED_REFS_RECOMPUTED_FROM_MARKERS)

    if sufficiency == "insufficient" and not missing_aspects:
        missing_aspects = [_COMPLETED_MISSING_ASPECT]
        defects.append(_DEFECT_MISSING_COMPLETED)

    draft = EvidenceAnswerDraft(
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


def _sufficiency_from_raw(value: object | None) -> EvidenceAnswerSufficiency:
    if value in ("answered", "insufficient"):
        return value
    raise EvidenceAnswerDraftInvalidError("unknown answer sufficiency")


def _clean_string_list(
    values: list[object],
    *,
    blank_defect: str,
    duplicate_defect: str,
    non_string_defect: str,
) -> tuple[list[str], list[str]]:
    result: list[str] = []
    seen: set[str] = set()
    defects: list[str] = []
    for value in values:
        if not isinstance(value, str):
            if non_string_defect not in defects:
                defects.append(non_string_defect)
            continue
        stripped = value.strip()
        if not stripped:
            if blank_defect not in defects:
                defects.append(blank_defect)
            continue
        if stripped in seen:
            if duplicate_defect not in defects:
                defects.append(duplicate_defect)
            continue
        result.append(stripped)
        seen.add(stripped)
    return result, defects


def _validate_draft_citations(
    *,
    evidence: list[AnswerEvidenceItem],
    draft: EvidenceAnswerDraft,
) -> None:
    existing_refs = {item.source.source_ref for item in evidence}
    unknown_refs = [ref for ref in draft.cited_refs if ref not in existing_refs]
    if unknown_refs:
        unknown_ref = unknown_refs[0]
        raise EvidenceAnswerDraftInvalidError(
            "answer 本文の citation marker "
            f"[[{unknown_ref}]] は evidence に存在しません"
        )
