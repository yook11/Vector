"""Question answering use-case orchestration."""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Protocol, assert_never

from app.agent.answering.direct_answer.contract import DirectAnswerer
from app.agent.answering.evidence_answer.contract import (
    EvidenceAnswerDraft,
    EvidenceAnswerDraftInvalidError,
    EvidenceAnswerer,
)
from app.agent.answering.evidence_answer.evidence import (
    AnswerEvidenceItem,
    normalize_answer_evidence,
)
from app.agent.contract import (
    AnswerProgressReporter,
    AnswerProgressStage,
    AnswerQuestionInput,
    AnswerQuestionResult,
    AnswerRetrievalSummary,
    AnswerSource,
    EvidenceCollectionFailure,
)
from app.agent.evidence_collection import EvidenceCollectionOutcome
from app.agent.planning.contract import (
    ExternalSearchPlan,
    InternalAndExternalPlan,
    InternalRetrievalPlan,
    NoRetrievalPlan,
    RetrievalPlan,
)
from app.agent.planning.planner import QuestionPlanner

__all__ = ["EvidenceCollector", "QuestionAnsweringOrchestrator"]

_RETRIEVAL_EMPTY_MISSING = "回答に使える根拠を取得できませんでした"
_COLLECTION_FAILURE_MISSING: dict[EvidenceCollectionFailure, str] = {
    "internal_search": "内部記事検索を完了できませんでした",
    "external_search": "外部検索を完了できませんでした",
}


class EvidenceCollector(Protocol):
    async def collect(
        self,
        plan: RetrievalPlan,
        *,
        as_of: datetime,
    ) -> EvidenceCollectionOutcome: ...


class QuestionAnsweringOrchestrator:
    """Top-level question answering use case."""

    def __init__(
        self,
        *,
        planner: QuestionPlanner,
        evidence_collector: EvidenceCollector,
        evidence_answerer: EvidenceAnswerer,
        direct_answerer: DirectAnswerer,
        progress: AnswerProgressReporter | None = None,
    ) -> None:
        self._planner = planner
        self._evidence_collector = evidence_collector
        self._evidence_answerer = evidence_answerer
        self._direct_answerer = direct_answerer
        self._progress = progress

    async def answer(self, input: AnswerQuestionInput) -> AnswerQuestionResult:
        await self._report_progress("planning")
        plan = await self._planner.plan(input)
        match plan:
            case NoRetrievalPlan():
                await self._report_progress("synthesizing")
                draft = await self._direct_answerer.answer(
                    question=input.question,
                    as_of=input.as_of,
                    user_intent=input.user_intent,
                    user_activity_context=input.user_activity_context,
                    previous_answer=input.previous_answer,
                )
                return AnswerQuestionResult(
                    status="answered",
                    answer=draft.answer,
                    sources=[],
                    missing_aspects=[],
                    retrieval=AnswerRetrievalSummary(
                        planned_mode="none",
                        collection_failures=[],
                    ),
                )
            case (
                InternalRetrievalPlan()
                | ExternalSearchPlan()
                | InternalAndExternalPlan()
            ):
                return await self._answer_with_evidence(input=input, plan=plan)
        assert_never(plan)

    async def _answer_with_evidence(
        self,
        *,
        input: AnswerQuestionInput,
        plan: RetrievalPlan,
    ) -> AnswerQuestionResult:
        await self._report_progress("retrieving")
        outcome = await self._evidence_collector.collect(plan, as_of=input.as_of)
        evidence = normalize_answer_evidence(outcome)

        await self._report_progress("synthesizing")
        draft = await self._evidence_answerer.answer(
            question=input.question,
            evidence=evidence,
            as_of=input.as_of,
            target_time_window=_plan_target_time_window(plan),
            user_intent=input.user_intent,
            prior_coverage=input.prior_coverage,
            user_activity_context=input.user_activity_context,
        )
        _validate_draft_citations(evidence=evidence, draft=draft)
        sources = _sources_for_citations(evidence=evidence, cited_refs=draft.cited_refs)

        return _assemble_evidence_result(
            plan=plan,
            outcome=outcome,
            answer=draft.answer,
            sources=sources,
            draft_missing_aspects=draft.missing_aspects,
            include_retrieval_empty_missing=not evidence,
        )

    async def _report_progress(self, stage: AnswerProgressStage) -> None:
        if self._progress is None:
            return
        await self._progress.stage_changed(stage)


def _plan_target_time_window(plan: RetrievalPlan) -> str | None:
    match plan:
        case ExternalSearchPlan() | InternalAndExternalPlan():
            return plan.target_time_window
        case InternalRetrievalPlan():
            return None
    assert_never(plan)


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
    plan: RetrievalPlan,
    outcome: EvidenceCollectionOutcome,
    answer: str,
    sources: list[AnswerSource],
    draft_missing_aspects: list[str],
    include_retrieval_empty_missing: bool,
) -> AnswerQuestionResult:
    missing_aspects = _missing_aspects(
        outcome=outcome,
        draft_missing_aspects=draft_missing_aspects,
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
        retrieval=AnswerRetrievalSummary(
            planned_mode=plan.retrieval_mode,
            collection_failures=outcome.collection_failures,
        ),
    )


def _derive_evidence_status(
    *,
    plan: RetrievalPlan,
    sources: list[AnswerSource],
    missing_aspects: list[str],
    outcome: EvidenceCollectionOutcome,
) -> Literal["answered", "insufficient"]:
    if outcome.collection_failures or missing_aspects:
        return "insufficient"
    if plan.retrieval_mode != "none" and not sources:
        return "insufficient"
    return "answered"


def _missing_aspects(
    *,
    outcome: EvidenceCollectionOutcome,
    draft_missing_aspects: list[str],
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
    return _deduplicate(values)


def _external_task_missing(outcome: EvidenceCollectionOutcome) -> list[str]:
    if outcome.external_search is None:
        return []
    missing: list[str] = []
    for report in sorted(
        outcome.external_search.task_reports,
        key=lambda report: report.task_index,
    ):
        missing.extend(report.missing)
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
