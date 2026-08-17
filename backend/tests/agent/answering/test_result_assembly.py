"""Result assemblyがEvidence answerの出力縮小(S4)後の契約を解釈するテスト。

`EvidenceAnswerDraft`はanswerとcited_refsだけを持つようになり、
`assemble_evidence_result()`からcontext引数(requirement参照が唯一の用途)が
消える。missing_aspectsは機構(retrieval empty / incomplete task / time filter /
review_missing)と生成不能の1行だけで組み立てられ、要望由来の文言は無くなる。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from app.agent.answering.evidence_answer.contract import (
    EvidenceAnswerDraft,
    EvidenceAnswerUnavailable,
)
from app.agent.answering.evidence_answer.evidence import AnswerInputEvidence
from app.agent.answering.result_assembly import (
    _UNAVAILABLE_ANSWER,
    _UNAVAILABLE_MISSING,
    assemble_evidence_result,
)
from app.agent.contract import InternalArticleSource
from app.agent.evidence_collection import (
    CollectedNews,
    CollectedTask,
    ResearchTaskReport,
)
from app.agent.evidence_review import (
    AnswerEvidence,
    EvidenceRunCompleted,
    EvidenceRunFailed,
    EvidenceRunResult,
)
from app.agent.evidence_review.answer_evidence import InternalArticleEvidence
from app.agent.planning.contract import (
    ExternalResearchTask,
    ResearchTask,
    SearchPlan,
    TargetTimeWindow,
)

_TIME_FILTER_MISSING = "指定された公開期間を外部検索へ適用できませんでした"
_INCOMPLETE_TASK_MISSING = "完了できなかった調査があります"


def _task(goal: str) -> ExternalResearchTask:
    return ExternalResearchTask(research_goal=goal)


def _search_plan(**payload: Any) -> SearchPlan:
    return SearchPlan(**payload)


def _research_tasks_from(
    tasks: list[ExternalResearchTask],
    *,
    query: str = "NVIDIA",
) -> list[ResearchTask]:
    """ExternalResearchTask(goalのみ)から、meaningを保ってResearchTaskへ配分する。"""
    return [
        ResearchTask(
            research_goal=task.research_goal,
            article_search_queries=[query],
        )
        for task in tasks
    ]


def _report(
    *,
    task_index: int,
    research_goal: str,
    internal_collection: str = "succeeded",
    external_collection: str = "succeeded",
    time_filter_failure_reason: str | None = None,
    generated_queries: list[str] | None = None,
    provider_failed_query_count: int = 0,
    internal_hit_count: int = 0,
    external_hit_count: int = 0,
) -> ResearchTaskReport:
    return ResearchTaskReport(
        task_index=task_index,
        research_goal=research_goal,
        internal_collection=internal_collection,
        external_collection=external_collection,
        time_filter_failure_reason=time_filter_failure_reason,
        generated_queries=generated_queries or [],
        provider_failed_query_count=provider_failed_query_count,
        internal_hit_count=internal_hit_count,
        external_hit_count=external_hit_count,
    )


def _time_filter_failed_report(
    *, task_index: int, research_goal: str, reason: str
) -> Any:
    """内部ヒットも無いtime filter失敗task(Run全体としてはskipped_empty相当)。"""
    return _report(
        task_index=task_index,
        research_goal=research_goal,
        internal_collection="succeeded",
        external_collection="time_filter_failed",
        time_filter_failure_reason=reason,
    )


def _collected_news(*, task_reports: list[ResearchTaskReport]) -> CollectedNews:
    return CollectedNews(
        tasks=[
            CollectedTask(
                task_index=report.task_index,
                research_goal=report.research_goal,
                internal_hits=[],
                external_hits=[],
                executed_queries=(),
                report=report,
            )
            for report in task_reports
        ],
        requested_agent_count=None,
        effective_agent_count=0,
    )


@dataclass(frozen=True)
class _AssemblyInput:
    collected_news: CollectedNews
    evidence_run: EvidenceRunResult


def _outcome(
    *,
    task_reports: list[ResearchTaskReport],
    review_missing: list[str] | None = None,
    failure_reason: str | None = None,
) -> _AssemblyInput:
    evidence_run: EvidenceRunResult
    if failure_reason is None:
        evidence_run = EvidenceRunCompleted(
            answer_evidence=AnswerEvidence(),
            review_missing=tuple(review_missing or []),
        )
    else:
        evidence_run = EvidenceRunFailed(failure_reason=failure_reason)
    return _AssemblyInput(
        collected_news=_collected_news(task_reports=task_reports),
        evidence_run=evidence_run,
    )


def _internal_evidence() -> AnswerInputEvidence:
    return AnswerInputEvidence(
        source=InternalArticleSource(
            source_ref="internal-1",
            article_id=1001,
            title="internal evidence",
        ),
        text="internal evidence",
    )


def _reviewed_internal_evidence(*, task_index: int) -> InternalArticleEvidence:
    return InternalArticleEvidence(
        option_index=task_index,
        task_index=task_index,
        claim="claim",
        why_selected="why",
        assessment_id=1001,
        curation_id=1,
        title="internal evidence",
        summary="summary",
    )


def _without_incomplete_phrase(missing_aspects: list[str]) -> list[str]:
    return [item for item in missing_aspects if item != _INCOMPLETE_TASK_MISSING]


def _unavailable(failure_code: str = "ai_error_network") -> EvidenceAnswerUnavailable:
    return EvidenceAnswerUnavailable(failure_code=failure_code)


def test_unavailable_wording_is_fixed_literal_text() -> None:
    """R4条件13・14: 生成不能時の定型文言を実値でリテラル固定する。

    他のテストは_UNAVAILABLE_ANSWER/_UNAVAILABLE_MISSING経由でこの定数を
    期待値として再利用しているため、文言そのものを変更しても緑のまま通って
    しまい、Invariant「定型本文とmissing_aspectsの文言は現行の値を維持する」の
    回帰保証にならない。ここでだけ実値をリテラルで固定し、値そのものの
    変更を検知できるようにする。
    """
    assert _UNAVAILABLE_ANSWER == (
        "回答を生成できませんでした。根拠の不足または応答形式の不備により、"
        "参考回答を安全に構築できませんでした。"
    )
    assert _UNAVAILABLE_MISSING == "回答生成に必要な根拠または応答形式が不足しました"


def _draft(*, answer: str, cited_refs: list[str] | None = None) -> EvidenceAnswerDraft:
    """EvidenceAnswerDraftはanswerとcited_refsだけを持つ

    (sufficiency/missing_aspects/unfulfilled_requirement_idsは撤去済み)。
    """
    return EvidenceAnswerDraft(answer=answer, cited_refs=cited_refs or [])


def _assemble(
    *,
    plan: SearchPlan,
    outcome: _AssemblyInput,
    evidence: list[AnswerInputEvidence],
    answer_outcome: EvidenceAnswerDraft | EvidenceAnswerUnavailable,
) -> Any:
    return assemble_evidence_result(
        plan=plan,
        collected_news=outcome.collected_news,
        evidence_run=outcome.evidence_run,
        evidence=evidence,
        answer_outcome=answer_outcome,
    )


def test_task_completes_via_internal_evidence_despite_external_provider_failure() -> (
    None
):
    """外部収集が失敗しても内部ヒットで精査が完了すれば

    経路名文言も固定文言も出ない(内部ヒットが残るためincomplete条件に
    当たらない。仕様「不足の表明」訂正)。
    """
    plan = _search_plan(
        research_tasks=_research_tasks_from(
            [ExternalResearchTask(research_goal="供給を確認する")]
        ),
        target_time_window=TargetTimeWindow(kind="last_n_days", days=1),
    )
    evidence = [
        AnswerInputEvidence(
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
                internal_hit_count=1,
            )
        ],
    )

    result = _assemble(
        plan=plan,
        outcome=outcome,
        evidence=evidence,
        answer_outcome=_draft(
            answer="内部根拠の範囲では確認できます。[[1]]",
            cited_refs=["1"],
        ),
    )

    assert (result.status, result.answer, result.missing_aspects) == (
        "answered",
        "内部根拠の範囲では確認できます。[[1]]",
        [],
    )
    assert not hasattr(result.plan_summary, "collection_failures")


def test_all_tasks_time_filter_failed_add_incomplete_and_time_filter_missing_once() -> (
    None
):
    """複数taskが未完了でも固定文言は1行に畳まれる

    (両taskとも内部ヒットゼロのためRun全体はskipped_empty相当)。
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
    )

    result = _assemble(
        plan=_search_plan(
            research_tasks=_research_tasks_from(tasks),
            target_time_window=TargetTimeWindow(kind="last_n_days", days=1),
        ),
        outcome=outcome,
        evidence=[],
        answer_outcome=_unavailable(),
    )

    assert result.status == "insufficient"
    assert result.missing_aspects.count(_INCOMPLETE_TASK_MISSING) == 1
    assert _without_incomplete_phrase(result.missing_aspects) == [
        "回答に使える根拠を取得できませんでした",
        _TIME_FILTER_MISSING,
        _UNAVAILABLE_MISSING,
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
    )

    result = _assemble(
        plan=_search_plan(
            research_tasks=_research_tasks_from(tasks),
            target_time_window=TargetTimeWindow(kind="last_n_days", days=1),
        ),
        outcome=outcome,
        evidence=evidence,
        answer_outcome=_draft(
            answer="内部根拠から確認できた範囲を回答します。[[internal-1]]",
            cited_refs=["internal-1"],
        ),
    )

    assert result.status == "insufficient"
    assert result.missing_aspects.count(_INCOMPLETE_TASK_MISSING) == 1
    assert _without_incomplete_phrase(result.missing_aspects) == [_TIME_FILTER_MISSING]


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
    )

    result = _assemble(
        plan=_search_plan(
            research_tasks=_research_tasks_from(tasks),
            target_time_window=TargetTimeWindow(kind="last_n_days", days=1),
        ),
        outcome=outcome,
        evidence=[],
        answer_outcome=_unavailable(),
    )

    assert result.status == "insufficient"
    assert result.missing_aspects.count(_INCOMPLETE_TASK_MISSING) == 1
    assert _without_incomplete_phrase(result.missing_aspects) == [
        "回答に使える根拠を取得できませんでした",
        _TIME_FILTER_MISSING,
        _UNAVAILABLE_MISSING,
    ]


def test_failed_evidence_run_adds_incomplete_missing_without_leaking_reason() -> None:
    """技術的な精査失敗は、ヒットがあっても回答を不完全として閉じる。"""
    tasks = [_task("直近の外部発表を確認する")]
    outcome = _outcome(
        task_reports=[
            _report(
                task_index=0,
                research_goal=tasks[0].research_goal,
                internal_hit_count=1,
            )
        ],
        failure_reason="response_not_json",
    )

    result = _assemble(
        plan=_search_plan(
            research_tasks=_research_tasks_from(tasks),
            target_time_window=TargetTimeWindow(kind="last_n_days", days=1),
        ),
        outcome=outcome,
        evidence=[],
        answer_outcome=_unavailable(),
    )

    assert (
        result.status,
        result.missing_aspects,
        "response_not_json" in result.missing_aspects,
    ) == (
        "insufficient",
        [
            "回答に使える根拠を取得できませんでした",
            _INCOMPLETE_TASK_MISSING,
            _UNAVAILABLE_MISSING,
        ],
        False,
    )


def test_assembly_rejects_completed_evidence_for_an_uncollected_task() -> None:
    tasks = [_task("直近の外部発表を確認する")]
    collected_news = _collected_news(
        task_reports=[
            _report(
                task_index=0,
                research_goal=tasks[0].research_goal,
                internal_hit_count=1,
            )
        ]
    )
    evidence_run = EvidenceRunCompleted(
        answer_evidence=AnswerEvidence(
            internal_evidence=(_reviewed_internal_evidence(task_index=1),),
        ),
        review_missing=(),
    )

    with pytest.raises(ValueError):
        assemble_evidence_result(
            plan=_search_plan(
                research_tasks=_research_tasks_from(tasks),
                target_time_window=TargetTimeWindow(kind="last_n_days", days=1),
            ),
            collected_news=collected_news,
            evidence_run=evidence_run,
            evidence=[],
            answer_outcome=_unavailable(),
        )


def test_internal_collection_failure_never_adds_a_route_name_phrase() -> None:
    """internal_collection=failedでも経路名文言は出ない

    (ヒットゼロ(internal_hit_count==0 かつ external_hit_count==0)
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
    )

    result = _assemble(
        plan=_search_plan(
            research_tasks=_research_tasks_from(tasks),
            target_time_window=TargetTimeWindow(kind="last_n_days", days=1),
        ),
        outcome=outcome,
        evidence=[],
        answer_outcome=_unavailable(),
    )

    assert result.status == "insufficient"
    assert "内部記事検索を完了できませんでした" not in result.missing_aspects
    assert "外部検索を完了できませんでした" not in result.missing_aspects
    assert result.missing_aspects.count(_INCOMPLETE_TASK_MISSING) == 1


def test_time_filter_failed_task_with_internal_evidence_stays_complete() -> None:
    """time filter文言は独立に出るが、内部ヒットが残っていれば

    (internal_hit_count>0)incomplete条件に当たらず固定文言は出ない。
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
                internal_hit_count=1,
            )
        ],
    )

    result = _assemble(
        plan=_search_plan(
            research_tasks=_research_tasks_from(tasks),
            target_time_window=TargetTimeWindow(kind="last_n_days", days=1),
        ),
        outcome=outcome,
        evidence=evidence,
        answer_outcome=_draft(
            answer="内部根拠から確認できた範囲を回答します。[[internal-1]]",
            cited_refs=["internal-1"],
        ),
    )

    assert result.status == "insufficient"
    assert _INCOMPLETE_TASK_MISSING not in result.missing_aspects
    assert result.missing_aspects == [_TIME_FILTER_MISSING]


def test_report_missing_deduplicates_against_review_missing() -> None:
    """missingはRun単位のreview_missingから1本だけ流れ、生成不能の1行と

    重複排除される(この task は provider_failed でヒットゼロだが、外部収集の
    失敗であり internal_hit_count も 0 のため incomplete条件(0/0)にも
    当たる)。
    """
    tasks = [_task("既存のexternal task")]
    shared_text = "共有される不足理由"
    unavailable_missing = _UNAVAILABLE_MISSING
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
        review_missing=[shared_text, unavailable_missing],
    )

    result = _assemble(
        plan=_search_plan(
            research_tasks=_research_tasks_from(tasks),
            target_time_window=None,
        ),
        outcome=outcome,
        evidence=[],
        answer_outcome=_unavailable(),
    )

    assert result.missing_aspects.count(_INCOMPLETE_TASK_MISSING) == 1
    assert _without_incomplete_phrase(result.missing_aspects) == [
        "回答に使える根拠を取得できませんでした",
        shared_text,
        unavailable_missing,
    ]
    assert result.missing_aspects.count(unavailable_missing) == 1


def test_unavailable_outcome_with_evidence_builds_insufficient_result() -> None:
    """生成不能でもevidenceがあるRunでresultが構築でき、sourcesは空、

    文言はresult_assembly側の定数から取られる(文言リテラルは複製しない)。
    """
    unavailable_answer = _UNAVAILABLE_ANSWER
    unavailable_missing = _UNAVAILABLE_MISSING
    tasks = [_task("直近の外部発表を確認する")]
    outcome = _outcome(
        task_reports=[
            _report(
                task_index=0,
                research_goal=tasks[0].research_goal,
                internal_hit_count=1,
            )
        ],
    )

    result = _assemble(
        plan=_search_plan(
            research_tasks=_research_tasks_from(tasks),
            target_time_window=None,
        ),
        outcome=outcome,
        evidence=[_internal_evidence()],
        answer_outcome=_unavailable(failure_code="ai_error_network"),
    )

    assert result.status == "insufficient"
    assert result.sources == []
    assert result.answer == unavailable_answer
    assert unavailable_missing in result.missing_aspects


def test_unavailable_missing_coexists_with_mechanism_missing_in_order() -> None:
    """機構由来のmissing_aspects(evidence空/incomplete task/time filter/

    review_missing)と生成不能の1行が併存し、現行どおりの順序で並ぶ。
    """
    unavailable_missing = _UNAVAILABLE_MISSING
    tasks = [_task("直近の外部発表を確認する")]
    outcome = _outcome(
        task_reports=[
            _time_filter_failed_report(
                task_index=0,
                research_goal=tasks[0].research_goal,
                reason="future_calendar_month",
            )
        ],
        review_missing=["reviewerが申告した不足"],
    )

    result = _assemble(
        plan=_search_plan(
            research_tasks=_research_tasks_from(tasks),
            target_time_window=TargetTimeWindow(kind="last_n_days", days=1),
        ),
        outcome=outcome,
        evidence=[],
        answer_outcome=_unavailable(failure_code="ai_error_network"),
    )

    assert result.status == "insufficient"
    assert result.missing_aspects.count(_INCOMPLETE_TASK_MISSING) == 1
    assert _without_incomplete_phrase(result.missing_aspects) == [
        "回答に使える根拠を取得できませんでした",
        _TIME_FILTER_MISSING,
        "reviewerが申告した不足",
        unavailable_missing,
    ]


def test_unavailable_missing_is_deduplicated_against_mechanism_missing() -> None:
    """生成不能の1行が機構由来のmissingと偶然一致しても重複排除される。"""
    unavailable_missing = _UNAVAILABLE_MISSING
    tasks = [_task("既存のexternal task")]
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
        review_missing=[unavailable_missing],
    )

    result = _assemble(
        plan=_search_plan(
            research_tasks=_research_tasks_from(tasks),
            target_time_window=None,
        ),
        outcome=outcome,
        evidence=[],
        answer_outcome=_unavailable(failure_code="ai_error_network"),
    )

    assert result.missing_aspects.count(unavailable_missing) == 1
