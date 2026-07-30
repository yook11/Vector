"""Result assembly が収集/精査分離後の新report shapeを解釈する契約(S1)。

`ResearchTaskReport`(収集系)と`EvidenceReviewReport`(Run単位の精査系)は
evidence_collection側のcontractへ再配置される想定(置き場は実装者が決めるため
facade経由で参照する)。production未実装のため新shapeを使うfixture構築自体が
red になる(ValidationError / AttributeError)。

不足の表明(D2)の発火条件は、現行のper-task review in (failed, skipped_empty)
と等価になるよう収集側から導出される: 任意のtaskでinternal_candidate_count==0
かつexternal_candidate_count==0(現行のskipped_empty相当)、または
outcome.review.review == "failed"(現行のper-task failed相当がRun全体へ移った
もの)。provider_failedのように候補ゼロでも収集自体は失敗していない外部分類は、
内部候補が残っていればincompleteにならない(仕様「不足の表明」訂正)。
"""

from __future__ import annotations

from typing import Any

import pytest

import app.agent.evidence_collection as evidence_collection_package
import app.agent.planning.contract as planning_contract
from app.agent.answering.evidence_answer.contract import EvidenceAnswerDraft
from app.agent.answering.evidence_answer.evidence import AnswerEvidenceItem
from app.agent.answering.result_assembly import assemble_evidence_result
from app.agent.contract import InternalArticleSource
from app.agent.evidence_collection import EvidenceCollectionOutcome
from app.agent.evidence_collection.external_search import ExternalSearchOutcome
from app.agent.planning.contract import (
    ExternalResearchTask,
    TargetTimeWindow,
)
from app.agent.question_context import AnswerRequirement, QuestionContext

_TIME_FILTER_MISSING = "指定された公開期間を外部検索へ適用できませんでした"
_INCOMPLETE_TASK_MISSING = "完了できなかった調査があります"


def _task(goal: str) -> ExternalResearchTask:
    return ExternalResearchTask(research_goal=goal)


def _search_plan(**payload: Any) -> object:
    plan_type = getattr(planning_contract, "SearchPlan", None)
    if plan_type is None:
        pytest.fail("planning contract must define SearchPlan")
    return plan_type(**payload)


def _research_tasks_from(
    tasks: list[ExternalResearchTask],
    *,
    query: str = "NVIDIA",
) -> list[Any]:
    """ExternalResearchTask(goalのみ)から、meaningを保ってResearchTaskへ配分する。"""
    research_task_type = getattr(planning_contract, "ResearchTask", None)
    if research_task_type is None:
        pytest.fail("planning contract must define ResearchTask")
    return [
        research_task_type(
            research_goal=task.research_goal,
            article_search_queries=[query],
        )
        for task in tasks
    ]


def _report_type() -> Any:
    report_type = getattr(evidence_collection_package, "ResearchTaskReport", None)
    if report_type is None:
        pytest.fail(
            "S1: app.agent.evidence_collection facade must export ResearchTaskReport"
        )
    return report_type


def _review_report_type() -> Any:
    review_report_type = getattr(
        evidence_collection_package, "EvidenceReviewReport", None
    )
    if review_report_type is None:
        pytest.fail(
            "S1: app.agent.evidence_collection facade must export EvidenceReviewReport"
        )
    return review_report_type


def _report(
    *,
    task_index: int,
    research_goal: str,
    internal_collection: str = "succeeded",
    external_collection: str = "succeeded",
    time_filter_failure_reason: str | None = None,
    generated_queries: list[str] | None = None,
    provider_failed_query_count: int = 0,
    internal_candidate_count: int = 0,
    external_candidate_count: int = 0,
) -> Any:
    """S1: ResearchTaskReportは収集系だけを持つ(review関連はEvidenceReviewReportへ)。

    このfileはmissing_aspects/statusの導出だけを対象にし、
    EvidenceCollectionOutcomeのΣ evidence_count整合はcontract testの責務とする。
    """
    report_type = _report_type()
    return report_type(
        task_index=task_index,
        research_goal=research_goal,
        internal_collection=internal_collection,
        external_collection=external_collection,
        time_filter_failure_reason=time_filter_failure_reason,
        generated_queries=generated_queries or [],
        provider_failed_query_count=provider_failed_query_count,
        internal_candidate_count=internal_candidate_count,
        external_candidate_count=external_candidate_count,
    )


def _review_report(
    *,
    review: str = "succeeded",
    review_failure_reason: str | None = None,
    internal_evidence_count: int = 0,
    external_evidence_count: int = 0,
    dropped_selection_count: int = 0,
    missing: list[str] | None = None,
) -> Any:
    review_report_type = _review_report_type()
    return review_report_type(
        review=review,
        review_failure_reason=review_failure_reason,
        internal_evidence_count=internal_evidence_count,
        external_evidence_count=external_evidence_count,
        dropped_selection_count=dropped_selection_count,
        missing=missing or [],
    )


def _time_filter_failed_report(
    *, task_index: int, research_goal: str, reason: str
) -> Any:
    """内部候補も無いtime filter失敗task(Run全体としてはskipped_empty相当)。"""
    return _report(
        task_index=task_index,
        research_goal=research_goal,
        internal_collection="succeeded",
        external_collection="time_filter_failed",
        time_filter_failure_reason=reason,
    )


def _outcome(
    *, task_reports: list[Any], review: Any | None = None
) -> EvidenceCollectionOutcome:
    return EvidenceCollectionOutcome(
        task_reports=task_reports,
        external_search=ExternalSearchOutcome(),
        review=review if review is not None else _review_report(),
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


def _without_incomplete_phrase(missing_aspects: list[str]) -> list[str]:
    return [item for item in missing_aspects if item != _INCOMPLETE_TASK_MISSING]


def test_task_completes_via_internal_evidence_despite_external_provider_failure() -> (
    None
):
    """保証するテスト条件 7。外部収集が失敗しても内部候補で精査が完了すれば

    経路名文言も固定文言も出ない(内部候補が残るためincomplete条件に
    当たらない。仕様「不足の表明」訂正)。
    """
    context = QuestionContext(standalone_question="NVIDIA の見通しは？")
    plan = _search_plan(
        research_tasks=_research_tasks_from(
            [ExternalResearchTask(research_goal="供給を確認する")]
        ),
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
    outcome = _outcome(
        task_reports=[
            _report(
                task_index=0,
                research_goal="供給を確認する",
                internal_collection="succeeded",
                external_collection="provider_failed",
                generated_queries=["NVIDIA 供給"],
                provider_failed_query_count=1,
                internal_candidate_count=1,
            )
        ],
        review=_review_report(review="succeeded"),
    )

    result = assemble_evidence_result(
        context=context,
        plan=plan,
        outcome=outcome,
        evidence=evidence,
        draft=EvidenceAnswerDraft(
            sufficiency="answered",
            answer="内部根拠の範囲では確認できます。",
            cited_refs=["1"],
        ),
    )

    assert (result.status, result.answer, result.missing_aspects) == (
        "answered",
        "内部根拠の範囲では確認できます。",
        [],
    )
    assert not hasattr(result.plan_summary, "collection_failures")


def test_all_tasks_time_filter_failed_add_incomplete_and_time_filter_missing_once() -> (
    None
):
    """保証するテスト条件 2。複数taskが未完了でも固定文言は1行に畳まれる

    (両taskとも内部候補ゼロのためRun全体はskipped_empty相当)。
    """
    tasks = [
        _task("Tavily 2027-08 の公開期間を確認する"),
        _task("provider 原典の公開期間を確認する"),
    ]
    outcome = _outcome(
        task_reports=[
            _time_filter_failed_report(
                task_index=index,
                research_goal=task.research_goal,
                reason=(
                    "future_calendar_month"
                    if index == 0
                    else "unsupported_explicit_window"
                ),
            )
            for index, task in enumerate(tasks)
        ],
        review=_review_report(review="skipped_empty"),
    )

    result = assemble_evidence_result(
        context=_context(
            content_requirements=["投資判断への影響"],
            response_requirements=["初心者向けの説明"],
        ),
        plan=_search_plan(
            research_tasks=_research_tasks_from(tasks),
            target_time_window=TargetTimeWindow(kind="last_n_days", days=1),
        ),
        outcome=outcome,
        evidence=[],
        draft=EvidenceAnswerDraft(
            sufficiency="insufficient",
            answer="外部根拠は取得できませんでした。",
            missing_aspects=["一般的な根拠不足"],
            unfulfilled_requirement_ids=["p1", "c1"],
        ),
    )

    assert result.status == "insufficient"
    assert result.missing_aspects.count(_INCOMPLETE_TASK_MISSING) == 1
    assert _without_incomplete_phrase(result.missing_aspects) == [
        "回答に使える根拠を取得できませんでした",
        _TIME_FILTER_MISSING,
        "回答要望を満たせませんでした: 投資判断への影響",
        "回答要望を満たせませんでした: 初心者向けの説明",
    ]


def test_time_filter_failure_task_incomplete_with_separate_evidence() -> None:
    tasks = [_task("直近の外部発表を確認する")]
    evidence = [_internal_evidence()]
    outcome = _outcome(
        task_reports=[
            _time_filter_failed_report(
                task_index=0,
                research_goal=tasks[0].research_goal,
                reason="future_calendar_month",
            )
        ],
        review=_review_report(review="skipped_empty"),
    )

    result = assemble_evidence_result(
        context=_context(
            content_requirements=["投資判断への影響"],
            response_requirements=["初心者向けの説明"],
        ),
        plan=_search_plan(
            research_tasks=_research_tasks_from(tasks),
            target_time_window=TargetTimeWindow(kind="last_n_days", days=1),
        ),
        outcome=outcome,
        evidence=evidence,
        draft=EvidenceAnswerDraft(
            sufficiency="insufficient",
            answer="内部根拠から確認できた範囲を回答します。",
            cited_refs=["internal-1"],
            missing_aspects=["内部根拠からは確認できない市場反応"],
            unfulfilled_requirement_ids=["c1", "p1"],
        ),
    )

    assert result.status == "insufficient"
    assert result.missing_aspects.count(_INCOMPLETE_TASK_MISSING) == 1
    assert _without_incomplete_phrase(result.missing_aspects) == [
        _TIME_FILTER_MISSING,
        "内部根拠からは確認できない市場反応",
        "回答要望を満たせませんでした: 投資判断への影響",
        "回答要望を満たせませんでした: 初心者向けの説明",
    ]


def test_empty_evidence_time_filter_failure_adds_incomplete_and_retrieval() -> None:
    tasks = [_task("直近の外部発表を確認する")]
    outcome = _outcome(
        task_reports=[
            _time_filter_failed_report(
                task_index=0,
                research_goal=tasks[0].research_goal,
                reason="future_calendar_month",
            )
        ],
        review=_review_report(review="skipped_empty"),
    )

    result = assemble_evidence_result(
        context=_context(content_requirements=["投資判断への影響"]),
        plan=_search_plan(
            research_tasks=_research_tasks_from(tasks),
            target_time_window=TargetTimeWindow(kind="last_n_days", days=1),
        ),
        outcome=outcome,
        evidence=[],
        draft=EvidenceAnswerDraft(
            sufficiency="insufficient",
            answer="根拠を十分に取得できませんでした。",
            missing_aspects=["一般的な根拠不足"],
            unfulfilled_requirement_ids=["c1"],
        ),
    )

    assert result.status == "insufficient"
    assert result.missing_aspects.count(_INCOMPLETE_TASK_MISSING) == 1
    assert _without_incomplete_phrase(result.missing_aspects) == [
        "回答に使える根拠を取得できませんでした",
        _TIME_FILTER_MISSING,
        "回答要望を満たせませんでした: 投資判断への影響",
    ]


def test_internal_collection_failure_never_adds_a_route_name_phrase() -> None:
    """保証するテスト条件 4。internal_collection=failedでも経路名文言は出ない

    (候補ゼロ(internal_candidate_count==0 かつ external_candidate_count==0)
    がRun全体のincomplete条件を導く)。
    """
    tasks = [_task("直近の外部発表を確認する")]
    outcome = _outcome(
        task_reports=[
            _report(
                task_index=0,
                research_goal=tasks[0].research_goal,
                internal_collection="failed",
                external_collection="time_filter_failed",
                time_filter_failure_reason="future_calendar_month",
            )
        ],
        review=_review_report(review="skipped_empty"),
    )

    result = assemble_evidence_result(
        context=_context(content_requirements=["投資判断への影響"]),
        plan=_search_plan(
            research_tasks=_research_tasks_from(tasks),
            target_time_window=TargetTimeWindow(kind="last_n_days", days=1),
        ),
        outcome=outcome,
        evidence=[],
        draft=EvidenceAnswerDraft(
            sufficiency="insufficient",
            answer="根拠を十分に取得できませんでした。",
            missing_aspects=["一般的な根拠不足"],
            unfulfilled_requirement_ids=["c1"],
        ),
    )

    assert result.status == "insufficient"
    assert "内部記事検索を完了できませんでした" not in result.missing_aspects
    assert "外部検索を完了できませんでした" not in result.missing_aspects
    assert result.missing_aspects.count(_INCOMPLETE_TASK_MISSING) == 1
    assert _without_incomplete_phrase(result.missing_aspects) == [
        "回答に使える根拠を取得できませんでした",
        _TIME_FILTER_MISSING,
        "回答要望を満たせませんでした: 投資判断への影響",
    ]


def test_time_filter_failed_task_with_internal_evidence_stays_complete() -> None:
    """保証するテスト条件 6。time filter文言は独立に出るが、内部候補が

    残っていれば(internal_candidate_count>0)incomplete条件に当たらず
    固定文言は出ない。
    """
    tasks = [_task("直近の外部発表を確認する")]
    evidence = [_internal_evidence()]
    outcome = _outcome(
        task_reports=[
            _report(
                task_index=0,
                research_goal=tasks[0].research_goal,
                internal_collection="succeeded",
                external_collection="time_filter_failed",
                time_filter_failure_reason="future_calendar_month",
                internal_candidate_count=1,
            )
        ],
        review=_review_report(review="succeeded"),
    )

    result = assemble_evidence_result(
        context=_context(content_requirements=["投資判断への影響"]),
        plan=_search_plan(
            research_tasks=_research_tasks_from(tasks),
            target_time_window=TargetTimeWindow(kind="last_n_days", days=1),
        ),
        outcome=outcome,
        evidence=evidence,
        draft=EvidenceAnswerDraft(
            # sufficiency="answered"でも、time filter文言とrequirement不足が
            # 非空になるためderive後のstatusはinsufficientへ強制される
            # (_derive_evidence_statusはmissing_aspects非空を優先する)。
            sufficiency="answered",
            answer="内部根拠から確認できた範囲を回答します。",
            cited_refs=["internal-1"],
            unfulfilled_requirement_ids=["c1"],
        ),
    )

    assert result.status == "insufficient"
    assert _INCOMPLETE_TASK_MISSING not in result.missing_aspects
    assert result.missing_aspects == [
        _TIME_FILTER_MISSING,
        "回答要望を満たせませんでした: 投資判断への影響",
    ]


def test_report_missing_deduplicates_against_draft_missing_aspects() -> None:
    """S1(不足の表明)。missingはRun単位のreview.missingから1本だけ流れる

    (旧: task別missingの連結・重複排除)。この task は provider_failed で
    候補ゼロだが、外部収集の失敗であり internal_candidate_count も 0 のため
    incomplete条件(0/0)にも当たる。
    """
    tasks = [_task("既存のexternal task")]
    shared_text = "共有される不足理由"
    outcome = _outcome(
        task_reports=[
            _report(
                task_index=0,
                research_goal=tasks[0].research_goal,
                external_collection="provider_failed",
                generated_queries=["既存のexternal task"],
                provider_failed_query_count=1,
            )
        ],
        review=_review_report(
            review="succeeded", missing=[shared_text, "report固有の不足"]
        ),
    )

    result = assemble_evidence_result(
        context=_context(),
        plan=_search_plan(
            research_tasks=_research_tasks_from(tasks),
            target_time_window=None,
        ),
        outcome=outcome,
        evidence=[],
        draft=EvidenceAnswerDraft(
            sufficiency="insufficient",
            answer="根拠が不足しています。",
            missing_aspects=[shared_text, "draft固有の不足"],
        ),
    )

    assert result.missing_aspects.count(_INCOMPLETE_TASK_MISSING) == 1
    assert _without_incomplete_phrase(result.missing_aspects) == [
        "回答に使える根拠を取得できませんでした",
        shared_text,
        "report固有の不足",
        "draft固有の不足",
    ]
