"""Result assembly が過去の external failure DTO を解釈する契約。"""

from __future__ import annotations

from typing import Any

import pytest

import app.agent.planning.contract as planning_contract
from app.agent.answering.evidence_answer.contract import EvidenceAnswerDraft
from app.agent.answering.evidence_answer.evidence import AnswerEvidenceItem
from app.agent.answering.result_assembly import assemble_evidence_result
from app.agent.contract import InternalArticleSource
from app.agent.evidence_collection import EvidenceCollectionOutcome
from app.agent.evidence_collection.external_search import (
    ExternalSearchOutcome,
    ResearchTaskReport,
)
from app.agent.planning.contract import (
    ExternalResearchTask,
    TargetTimeWindow,
)
from app.agent.question_context import AnswerRequirement, QuestionContext

_TIME_FILTER_MISSING = "指定された公開期間を外部検索へ適用できませんでした"


def _task(goal: str) -> ExternalResearchTask:
    return ExternalResearchTask(research_goal=goal)


def _search_plan(**payload: Any) -> object:
    plan_type = getattr(planning_contract, "SearchPlan", None)
    if plan_type is None:
        pytest.fail("planning contract must define SearchPlan")
    return plan_type(**payload)


def _time_filter_outcome(
    tasks: list[ExternalResearchTask],
) -> ExternalSearchOutcome:
    return ExternalSearchOutcome(
        tasks=tasks,
        task_reports=[
            ResearchTaskReport(
                task_index=index,
                research_goal=task.research_goal,
                status="time_filter_failed",
                time_filter_failure_reason=(
                    "future_calendar_month"
                    if index == 0
                    else "unsupported_explicit_window"
                ),
            )
            for index, task in enumerate(tasks)
        ],
    )


def _context(
    *,
    content_requirements: list[str] | None = None,
    response_requirements: list[str] | None = None,
) -> QuestionContext:
    return QuestionContext(
        standalone_question="NVIDIA の見通しは？",
        content_requirements=[
            AnswerRequirement(requirement_id=f"c{index}", description=value)
            for index, value in enumerate(content_requirements or [], start=1)
        ],
        response_requirements=[
            AnswerRequirement(requirement_id=f"p{index}", description=value)
            for index, value in enumerate(response_requirements or [], start=1)
        ],
    )


def _internal_evidence() -> AnswerEvidenceItem:
    return AnswerEvidenceItem(
        source=InternalArticleSource(
            source_ref="internal-1",
            article_id=1001,
            title="internal evidence",
        ),
        text="internal evidence",
    )


def test_assembly_caps_answered_draft_for_historical_external_failure() -> None:
    context = QuestionContext(standalone_question="NVIDIA の見通しは？")
    plan = _search_plan(
        article_search_queries=["NVIDIA"],
        external_research_tasks=[ExternalResearchTask(research_goal="供給を確認する")],
        target_time_window=TargetTimeWindow(kind="last_n_days", days=1),
    )
    evidence = [
        AnswerEvidenceItem(
            source=InternalArticleSource(
                source_ref="1",
                article_id=1001,
                title="internal evidence",
            ),
            text="internal evidence",
        )
    ]

    result = assemble_evidence_result(
        context=context,
        plan=plan,
        outcome=EvidenceCollectionOutcome(collection_failures=["external_search"]),
        evidence=evidence,
        draft=EvidenceAnswerDraft(
            sufficiency="answered",
            answer="内部根拠の範囲では確認できます。",
            cited_refs=["1"],
        ),
    )

    assert (
        result.status,
        result.answer,
        result.plan_summary.collection_failures,
        result.missing_aspects,
    ) == (
        "insufficient",
        "内部根拠の範囲では確認できます。",
        ["external_search"],
        ["外部検索を完了できませんでした"],
    )


def test_search_time_filter_failure_keeps_one_missing_and_requirements() -> None:
    tasks = [
        _task("Tavily 2027-08 の公開期間を確認する"),
        _task("provider 原典の公開期間を確認する"),
    ]
    result = assemble_evidence_result(
        context=_context(
            content_requirements=["投資判断への影響"],
            response_requirements=["初心者向けの説明"],
        ),
        plan=_search_plan(
            article_search_queries=["NVIDIA"],
            external_research_tasks=tasks,
            target_time_window=TargetTimeWindow(kind="last_n_days", days=1),
        ),
        outcome=EvidenceCollectionOutcome(
            external_search=_time_filter_outcome(tasks),
        ),
        evidence=[],
        draft=EvidenceAnswerDraft(
            sufficiency="insufficient",
            answer="外部根拠は取得できませんでした。",
            missing_aspects=["一般的な根拠不足"],
            unfulfilled_requirement_ids=["p1", "c1"],
        ),
    )

    assert (result.status, result.missing_aspects) == (
        "insufficient",
        [
            "回答に使える根拠を取得できませんでした",
            _TIME_FILTER_MISSING,
            "回答要望を満たせませんでした: 投資判断への影響",
            "回答要望を満たせませんでした: 初心者向けの説明",
        ],
    )


def test_search_time_filter_failure_keeps_independent_draft_and_requirements() -> None:
    tasks = [_task("直近の外部発表を確認する")]
    evidence = [_internal_evidence()]
    result = assemble_evidence_result(
        context=_context(
            content_requirements=["投資判断への影響"],
            response_requirements=["初心者向けの説明"],
        ),
        plan=_search_plan(
            article_search_queries=["NVIDIA"],
            external_research_tasks=tasks,
            target_time_window=TargetTimeWindow(kind="last_n_days", days=1),
        ),
        outcome=EvidenceCollectionOutcome(
            external_search=_time_filter_outcome(tasks),
        ),
        evidence=evidence,
        draft=EvidenceAnswerDraft(
            sufficiency="insufficient",
            answer="内部根拠から確認できた範囲を回答します。",
            cited_refs=["internal-1"],
            missing_aspects=["内部根拠からは確認できない市場反応"],
            unfulfilled_requirement_ids=["c1", "p1"],
        ),
    )

    assert (result.status, result.missing_aspects) == (
        "insufficient",
        [
            _TIME_FILTER_MISSING,
            "内部根拠からは確認できない市場反応",
            "回答要望を満たせませんでした: 投資判断への影響",
            "回答要望を満たせませんでした: 初心者向けの説明",
        ],
    )


def test_empty_search_evidence_keeps_retrieval_missing_with_time_filter_missing() -> (
    None
):
    tasks = [_task("直近の外部発表を確認する")]
    result = assemble_evidence_result(
        context=_context(content_requirements=["投資判断への影響"]),
        plan=_search_plan(
            article_search_queries=["NVIDIA"],
            external_research_tasks=tasks,
            target_time_window=TargetTimeWindow(kind="last_n_days", days=1),
        ),
        outcome=EvidenceCollectionOutcome(
            external_search=_time_filter_outcome(tasks),
        ),
        evidence=[],
        draft=EvidenceAnswerDraft(
            sufficiency="insufficient",
            answer="根拠を十分に取得できませんでした。",
            missing_aspects=["一般的な根拠不足"],
            unfulfilled_requirement_ids=["c1"],
        ),
    )

    assert (result.status, result.missing_aspects) == (
        "insufficient",
        [
            "回答に使える根拠を取得できませんでした",
            _TIME_FILTER_MISSING,
            "回答要望を満たせませんでした: 投資判断への影響",
        ],
    )


def test_empty_search_evidence_keeps_internal_failure_and_time_filter_missing() -> None:
    tasks = [_task("直近の外部発表を確認する")]
    result = assemble_evidence_result(
        context=_context(content_requirements=["投資判断への影響"]),
        plan=_search_plan(
            article_search_queries=["NVIDIA"],
            external_research_tasks=tasks,
            target_time_window=TargetTimeWindow(kind="last_n_days", days=1),
        ),
        outcome=EvidenceCollectionOutcome(
            external_search=_time_filter_outcome(tasks),
            collection_failures=["internal_search"],
        ),
        evidence=[],
        draft=EvidenceAnswerDraft(
            sufficiency="insufficient",
            answer="根拠を十分に取得できませんでした。",
            missing_aspects=["一般的な根拠不足"],
            unfulfilled_requirement_ids=["c1"],
        ),
    )

    assert (result.status, result.missing_aspects) == (
        "insufficient",
        [
            "回答に使える根拠を取得できませんでした",
            "内部記事検索を完了できませんでした",
            _TIME_FILTER_MISSING,
            "回答要望を満たせませんでした: 投資判断への影響",
        ],
    )


def test_non_time_filter_report_missing_keeps_existing_canonical_deduplication() -> (
    None
):
    tasks = [_task("既存のexternal task")]
    result = assemble_evidence_result(
        context=_context(),
        plan=_search_plan(
            article_search_queries=["NVIDIA"],
            external_research_tasks=tasks,
            target_time_window=None,
        ),
        outcome=EvidenceCollectionOutcome(
            external_search=ExternalSearchOutcome(
                tasks=tasks,
                task_reports=[
                    ResearchTaskReport(
                        task_index=0,
                        research_goal=tasks[0].research_goal,
                        status="provider_failed",
                        missing=[
                            "既存の外部不足",
                            "内部記事検索を完了できませんでした",
                        ],
                    )
                ],
            ),
            collection_failures=["internal_search"],
        ),
        evidence=[],
        draft=EvidenceAnswerDraft(
            sufficiency="insufficient",
            answer="根拠が不足しています。",
            missing_aspects=["既存の外部不足", "draft固有の不足"],
        ),
    )

    assert result.missing_aspects == [
        "回答に使える根拠を取得できませんでした",
        "内部記事検索を完了できませんでした",
        "既存の外部不足",
        "draft固有の不足",
    ]
