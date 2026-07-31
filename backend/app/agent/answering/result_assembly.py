"""Evidence回答の検証と最終result組み立て。"""

from __future__ import annotations

from typing import Literal

from app.agent.answering.evidence_answer.contract import (
    EvidenceAnswerDraft,
    EvidenceAnswerDraftInvalidError,
    EvidenceAnswerOutcome,
    EvidenceAnswerUnavailable,
)
from app.agent.answering.evidence_answer.evidence import AnswerEvidenceItem
from app.agent.contract import (
    AnswerPlanSummary,
    AnswerQuestionResult,
    AnswerSource,
)
from app.agent.evidence_collection import EvidenceCollectionOutcome
from app.agent.planning.contract import SearchPlan

__all__ = ["assemble_evidence_result"]

_RETRIEVAL_EMPTY_MISSING = "回答に使える根拠を取得できませんでした"
_INCOMPLETE_TASK_MISSING = "完了できなかった調査があります"
_EXTERNAL_TASK_STATUS_MISSING = {
    "time_filter_failed": "指定された公開期間を外部検索へ適用できませんでした",
}
_UNAVAILABLE_ANSWER = (
    "回答を生成できませんでした。根拠の不足または応答形式の不備により、"
    "参考回答を安全に構築できませんでした。"
)
_UNAVAILABLE_MISSING = "回答生成に必要な根拠または応答形式が不足しました"


def assemble_evidence_result(
    *,
    plan: SearchPlan,
    outcome: EvidenceCollectionOutcome,
    evidence: list[AnswerEvidenceItem],
    answer_outcome: EvidenceAnswerOutcome,
) -> AnswerQuestionResult:
    if isinstance(answer_outcome, EvidenceAnswerUnavailable):
        return _assemble_evidence_result(
            plan=plan,
            outcome=outcome,
            answer=_UNAVAILABLE_ANSWER,
            sources=[],
            unavailable_missing=[_UNAVAILABLE_MISSING],
            include_retrieval_empty_missing=(not evidence),
        )

    draft = answer_outcome
    _validate_draft_citations(evidence=evidence, draft=draft)
    sources = _sources_for_citations(evidence=evidence, cited_refs=draft.cited_refs)
    return _assemble_evidence_result(
        plan=plan,
        outcome=outcome,
        answer=draft.answer,
        sources=sources,
        unavailable_missing=[],
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
    unavailable_missing: list[str],
    include_retrieval_empty_missing: bool,
) -> AnswerQuestionResult:
    missing_aspects = _missing_aspects(
        outcome=outcome,
        unavailable_missing=unavailable_missing,
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
    outcome: EvidenceCollectionOutcome,
    unavailable_missing: list[str],
    include_retrieval_empty_missing: bool,
) -> list[str]:
    values: list[str] = []
    if include_retrieval_empty_missing:
        values.append(_RETRIEVAL_EMPTY_MISSING)
    if _has_incomplete_task(outcome):
        values.append(_INCOMPLETE_TASK_MISSING)
    values.extend(_external_task_status_missing(outcome))
    values.extend(outcome.review.missing)
    values.extend(unavailable_missing)
    return _deduplicate(values)


def _has_incomplete_task(outcome: EvidenceCollectionOutcome) -> bool:
    if outcome.review.review == "failed":
        return True
    return any(
        report.internal_candidate_count == 0 and report.external_candidate_count == 0
        for report in outcome.task_reports
    )


def _external_task_status_missing(outcome: EvidenceCollectionOutcome) -> list[str]:
    """収集の失敗表明(time filter文言)だけをtask単位で連結する。

    Run全体の不足(missing)はreviewerがRun単位で1本返すため、
    ここでtask別に連結しない(仕様「何ができていないかの表明」)。
    """
    missing: list[str] = []
    for report in sorted(
        outcome.task_reports,
        key=lambda report: report.task_index,
    ):
        status_missing = _EXTERNAL_TASK_STATUS_MISSING.get(report.external_collection)
        if status_missing is not None:
            missing.append(status_missing)
    return missing


def _deduplicate(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value in seen:
            continue
        result.append(value)
        seen.add(value)
    return result
