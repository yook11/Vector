"""Deterministic evidence answer draft finalization."""

from __future__ import annotations

from app.agent.answering.evidence_answer.contract import (
    EvidenceAnswerDraft,
    EvidenceAnswerDraftInvalidError,
)
from app.agent.answering.evidence_answer.evidence import AnswerEvidenceItem
from app.agent.citation_markers import parse_citation_refs

__all__ = ["finalize_evidence_answer_draft"]


def finalize_evidence_answer_draft(
    answer: str,
    *,
    evidence: list[AnswerEvidenceItem],
) -> EvidenceAnswerDraft:
    """plain textの回答本文からcited_refsを決定的に算出し、接地契約を検証する。"""

    if not answer.strip():
        raise EvidenceAnswerDraftInvalidError("answer body must not be blank")

    cited_refs = list(parse_citation_refs(answer))
    if evidence and not cited_refs:
        raise EvidenceAnswerDraftInvalidError(
            "answer requires at least one citation marker when evidence exists"
        )

    draft = EvidenceAnswerDraft(answer=answer, cited_refs=cited_refs)
    _validate_draft_citations(evidence=evidence, draft=draft)
    return draft


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
