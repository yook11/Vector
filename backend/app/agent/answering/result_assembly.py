"""Evidence回答の検証と最終result組み立て。"""

from __future__ import annotations

from typing import Literal

from app.agent.answering.evidence_answer.contract import (
    EvidenceAnswerDraft,
    EvidenceAnswerDraftInvalidError,
)
from app.agent.answering.evidence_answer.evidence import AnswerInputEvidence
from app.agent.contract import (
    AnswerPlanSummary,
    AnswerQuestionResult,
    AnswerSource,
)
from app.agent.evidence_collection import CollectedNews
from app.agent.evidence_review import (
    EvidenceRunCompleted,
    EvidenceRunFailed,
    EvidenceRunResult,
)
from app.agent.planning.contract import SearchPlan

__all__ = ["assemble_evidence_result"]

_RETRIEVAL_EMPTY_MISSING = "回答に使える根拠を取得できませんでした"
_INCOMPLETE_TASK_MISSING = "完了できなかった調査があります"


def assemble_evidence_result(
    *,
    plan: SearchPlan,
    collected_news: CollectedNews,
    evidence_run: EvidenceRunResult,
    evidence: list[AnswerInputEvidence],
    answer_outcome: EvidenceAnswerDraft,
) -> AnswerQuestionResult:
    if isinstance(evidence_run, EvidenceRunCompleted):
        collected_task_indexes = {task.task_index for task in collected_news.tasks}
        if not evidence_run.answer_evidence.task_indexes <= collected_task_indexes:
            raise ValueError("answer evidence must reference a collected task")

    draft = answer_outcome
    _validate_draft_citations(evidence=evidence, draft=draft)
    sources = _sources_for_citations(evidence=evidence, cited_refs=draft.cited_refs)
    return _assemble_evidence_result(
        plan=plan,
        collected_news=collected_news,
        evidence_run=evidence_run,
        answer=draft.answer,
        sources=sources,
        include_retrieval_empty_missing=(not evidence),
    )


def _validate_draft_citations(
    *,
    evidence: list[AnswerInputEvidence],
    draft: EvidenceAnswerDraft,
) -> None:
    existing_refs = {item.source.source_ref for item in evidence}
    unknown_refs = [ref for ref in draft.cited_refs if ref not in existing_refs]
    if unknown_refs:
        raise EvidenceAnswerDraftInvalidError(
            f"unknown citation ref: {unknown_refs[0]}"
        )


def _sources_for_citations(
    *,
    evidence: list[AnswerInputEvidence],
    cited_refs: list[str],
) -> list[AnswerSource]:
    cited_ref_set = set(cited_refs)
    return [item.source for item in evidence if item.source.source_ref in cited_ref_set]


def _assemble_evidence_result(
    *,
    plan: SearchPlan,
    collected_news: CollectedNews,
    evidence_run: EvidenceRunResult,
    answer: str,
    sources: list[AnswerSource],
    include_retrieval_empty_missing: bool,
) -> AnswerQuestionResult:
    missing_aspects = _missing_aspects(
        collected_news=collected_news,
        evidence_run=evidence_run,
        include_retrieval_empty_missing=include_retrieval_empty_missing,
    )
    status = _derive_evidence_status(sources=sources, missing_aspects=missing_aspects)

    return AnswerQuestionResult(
        status=status,
        answer=answer,
        sources=sources,
        missing_aspects=missing_aspects,
        plan_summary=AnswerPlanSummary(plan_type=plan.plan_type),
    )


def _derive_evidence_status(
    *,
    sources: list[AnswerSource],
    missing_aspects: list[str],
) -> Literal["answered", "insufficient"]:
    if missing_aspects:
        return "insufficient"
    if not sources:
        return "insufficient"
    return "answered"


def _missing_aspects(
    *,
    collected_news: CollectedNews,
    evidence_run: EvidenceRunResult,
    include_retrieval_empty_missing: bool,
) -> list[str]:
    values: list[str] = []
    if include_retrieval_empty_missing:
        values.append(_RETRIEVAL_EMPTY_MISSING)
    if _has_incomplete_task(
        collected_news=collected_news,
        evidence_run=evidence_run,
    ):
        values.append(_INCOMPLETE_TASK_MISSING)
    if isinstance(evidence_run, EvidenceRunCompleted):
        values.extend(evidence_run.review_missing)
    return _deduplicate(values)


def _has_incomplete_task(
    *,
    collected_news: CollectedNews,
    evidence_run: EvidenceRunResult,
) -> bool:
    if isinstance(evidence_run, EvidenceRunFailed):
        return True
    return any(
        task.report.internal_hit_count == 0 and task.report.external_hit_count == 0
        for task in collected_news.tasks
    )


def _deduplicate(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value in seen:
            continue
        result.append(value)
        seen.add(value)
    return result
