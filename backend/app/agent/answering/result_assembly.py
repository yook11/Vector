"""Evidence回答の検証と最終result組み立て。"""

from __future__ import annotations

from typing import Literal

from app.agent.answering.evidence_answer.contract import (
    EvidenceAnswerDraft,
    EvidenceAnswerDraftInvalidError,
)
from app.agent.answering.evidence_answer.evidence import AnswerEvidenceItem
from app.agent.contract import (
    AnswerPlanSummary,
    AnswerQuestionResult,
    AnswerSource,
    EvidenceCollectionFailure,
)
from app.agent.evidence_collection import EvidenceCollectionOutcome
from app.agent.planning.contract import SearchPlan
from app.agent.question_context.contract import QuestionContext

__all__ = ["assemble_evidence_result"]

_RETRIEVAL_EMPTY_MISSING = "回答に使える根拠を取得できませんでした"
_REQUIREMENT_MISSING_PREFIX = "回答要望を満たせませんでした: "
_COLLECTION_FAILURE_MISSING: dict[EvidenceCollectionFailure, str] = {
    "internal_search": "内部記事検索を完了できませんでした",
    "external_search": "外部検索を完了できませんでした",
}
_EXTERNAL_TASK_STATUS_MISSING = {
    "time_filter_failed": "指定された公開期間を外部検索へ適用できませんでした",
}


def assemble_evidence_result(
    *,
    context: QuestionContext,
    plan: SearchPlan,
    outcome: EvidenceCollectionOutcome,
    evidence: list[AnswerEvidenceItem],
    draft: EvidenceAnswerDraft,
) -> AnswerQuestionResult:
    _validate_draft_citations(evidence=evidence, draft=draft)
    requirement_missing_aspects = _unfulfilled_requirement_missing_aspects(
        context=context,
        requirement_ids=draft.unfulfilled_requirement_ids,
    )
    sources = _sources_for_citations(evidence=evidence, cited_refs=draft.cited_refs)
    all_external_tasks_time_filter_failed = _all_external_tasks_time_filter_failed(
        outcome
    )
    return _assemble_evidence_result(
        plan=plan,
        outcome=outcome,
        answer=draft.answer,
        sources=sources,
        draft_missing_aspects=(
            []
            if not evidence and all_external_tasks_time_filter_failed
            else draft.missing_aspects
        ),
        requirement_missing_aspects=requirement_missing_aspects,
        include_retrieval_empty_missing=(not evidence),
    )


def _validate_draft_citations(
    *,
    evidence: list[AnswerEvidenceItem],
    draft: EvidenceAnswerDraft,
) -> None:
    existing_refs = {item.source.source_ref for item in evidence}
    unknown_refs = [ref for ref in draft.cited_refs if ref not in existing_refs]
    if unknown_refs:
        raise EvidenceAnswerDraftInvalidError(
            f"unknown citation ref: {unknown_refs[0]}"
        )


def _unfulfilled_requirement_missing_aspects(
    *,
    context: QuestionContext,
    requirement_ids: list[str],
) -> list[str]:
    requirements = [
        *context.content_requirements,
        *context.response_requirements,
    ]
    known_ids = {requirement.requirement_id for requirement in requirements}
    if any(requirement_id not in known_ids for requirement_id in requirement_ids):
        raise EvidenceAnswerDraftInvalidError("unknown unfulfilled requirement id")

    unfulfilled_ids = set(requirement_ids)
    return [
        f"{_REQUIREMENT_MISSING_PREFIX}{requirement.description}"
        for requirement in requirements
        if requirement.requirement_id in unfulfilled_ids
    ]


def _sources_for_citations(
    *,
    evidence: list[AnswerEvidenceItem],
    cited_refs: list[str],
) -> list[AnswerSource]:
    cited_ref_set = set(cited_refs)
    return [item.source for item in evidence if item.source.source_ref in cited_ref_set]


def _assemble_evidence_result(
    *,
    plan: SearchPlan,
    outcome: EvidenceCollectionOutcome,
    answer: str,
    sources: list[AnswerSource],
    draft_missing_aspects: list[str],
    requirement_missing_aspects: list[str],
    include_retrieval_empty_missing: bool,
) -> AnswerQuestionResult:
    missing_aspects = _missing_aspects(
        outcome=outcome,
        draft_missing_aspects=draft_missing_aspects,
        requirement_missing_aspects=requirement_missing_aspects,
        include_retrieval_empty_missing=include_retrieval_empty_missing,
    )
    status = _derive_evidence_status(
        plan=plan,
        sources=sources,
        missing_aspects=missing_aspects,
        outcome=outcome,
    )
    if status == "answered":
        missing_aspects = []

    return AnswerQuestionResult(
        status=status,
        answer=answer,
        sources=sources,
        missing_aspects=missing_aspects,
        plan_summary=AnswerPlanSummary(
            plan_type=plan.plan_type,
            collection_failures=outcome.collection_failures,
        ),
    )


def _derive_evidence_status(
    *,
    plan: SearchPlan,
    sources: list[AnswerSource],
    missing_aspects: list[str],
    outcome: EvidenceCollectionOutcome,
) -> Literal["answered", "insufficient"]:
    if outcome.collection_failures or missing_aspects:
        return "insufficient"
    if not sources:
        return "insufficient"
    return "answered"


def _missing_aspects(
    *,
    outcome: EvidenceCollectionOutcome,
    draft_missing_aspects: list[str],
    requirement_missing_aspects: list[str],
    include_retrieval_empty_missing: bool,
) -> list[str]:
    values: list[str] = []
    if include_retrieval_empty_missing:
        values.append(_RETRIEVAL_EMPTY_MISSING)
    values.extend(
        _COLLECTION_FAILURE_MISSING[failure] for failure in outcome.collection_failures
    )
    values.extend(_external_task_missing(outcome))
    values.extend(draft_missing_aspects)
    values.extend(requirement_missing_aspects)
    return _deduplicate(values)


def _external_task_missing(outcome: EvidenceCollectionOutcome) -> list[str]:
    if outcome.external_search is None:
        return []
    missing: list[str] = []
    for report in sorted(
        outcome.external_search.task_reports,
        key=lambda report: report.task_index,
    ):
        status_missing = _EXTERNAL_TASK_STATUS_MISSING.get(report.status)
        if status_missing is not None:
            missing.append(status_missing)
        missing.extend(report.missing)
    return missing


def _all_external_tasks_time_filter_failed(
    outcome: EvidenceCollectionOutcome,
) -> bool:
    external_search = outcome.external_search
    return (
        external_search is not None
        and bool(external_search.task_reports)
        and all(
            report.status == "time_filter_failed"
            for report in external_search.task_reports
        )
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
