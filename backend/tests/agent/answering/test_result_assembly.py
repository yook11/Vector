"""Result assemblyがEvidence answerの出力縮小(S4)後の契約を解釈するテスト。

`EvidenceAnswerDraft`はanswerとcited_refsだけを持つようになり、
`assemble_evidence_result()`からcontext引数(requirement参照が唯一の用途)が
消える。missing_aspectsは機構(retrieval empty / incomplete task /
review_missing)から組み立てられ、生成失敗はこの工程に渡らない。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from app.agent.answering.evidence_answer.contract import (
    EvidenceAnswerDraft,
)
from app.agent.answering.evidence_answer.evidence import AnswerInputEvidence
from app.agent.answering.result_assembly import (
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
        generated_queries=generated_queries or [],
        provider_failed_query_count=provider_failed_query_count,
        internal_hit_count=internal_hit_count,
        external_hit_count=external_hit_count,
    )


def _zero_hit_report(*, task_index: int, research_goal: str) -> Any:
    """内部も外部もヒットが無いtask(incomplete条件)。"""
    return _report(
        task_index=task_index,
        research_goal=research_goal,
        internal_collection="succeeded",
        external_collection="succeeded",
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
    )


@dataclass(frozen=True)
class _AssemblyInput:
    collected_news: CollectedNews
    evidence_run: EvidenceRunResult


def _outcome(
    *,
    task_reports: list[ResearchTaskReport],
    review_missing: list[str] | None = None,
    failure_code: str | None = None,
) -> _AssemblyInput:
    evidence_run: EvidenceRunResult
    if failure_code is None:
        evidence_run = EvidenceRunCompleted(
            answer_evidence=AnswerEvidence(),
            review_missing=tuple(review_missing or []),
        )
    else:
        evidence_run = EvidenceRunFailed(failure_code=failure_code)
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
    answer_outcome: EvidenceAnswerDraft,
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


def test_all_tasks_zero_hits_add_incomplete_missing_once() -> None:
    """複数taskが未完了でも固定文言は1行に畳まれる。"""
    tasks = [
        _task("Tavily 2027-08 の公開期間を確認する"),
        _task("provider 原典の公開期間を確認する"),
    ]
    outcome = _outcome(
        task_reports=[
            _zero_hit_report(
                task_index=index,
                research_goal=task.research_goal,
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
        answer_outcome=_draft(answer="確認できた範囲で回答します。"),
    )

    assert result.status == "insufficient"
    assert result.missing_aspects.count(_INCOMPLETE_TASK_MISSING) == 1
    assert _without_incomplete_phrase(result.missing_aspects) == [
        "回答に使える根拠を取得できませんでした",
    ]


def test_zero_hit_task_incomplete_with_separate_evidence() -> None:
    tasks = [_task("直近の外部発表を確認する")]
    evidence = [_internal_evidence()]
    outcome = _outcome(
        task_reports=[
            _zero_hit_report(
                task_index=0,
                research_goal=tasks[0].research_goal,
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
    assert _without_incomplete_phrase(result.missing_aspects) == []


def test_empty_evidence_zero_hits_adds_incomplete_and_retrieval() -> None:
    tasks = [_task("直近の外部発表を確認する")]
    outcome = _outcome(
        task_reports=[
            _zero_hit_report(
                task_index=0,
                research_goal=tasks[0].research_goal,
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
        answer_outcome=_draft(answer="確認できた範囲で回答します。"),
    )

    assert result.status == "insufficient"
    assert result.missing_aspects.count(_INCOMPLETE_TASK_MISSING) == 1
    assert _without_incomplete_phrase(result.missing_aspects) == [
        "回答に使える根拠を取得できませんでした",
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
        failure_code="response_not_json",
    )

    result = _assemble(
        plan=_search_plan(
            research_tasks=_research_tasks_from(tasks),
            target_time_window=TargetTimeWindow(kind="last_n_days", days=1),
        ),
        outcome=outcome,
        evidence=[],
        answer_outcome=_draft(answer="確認できた範囲で回答します。"),
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
            answer_outcome=_draft(answer="確認できた範囲で回答します。"),
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
                external_collection="query_generation_failed",
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
        answer_outcome=_draft(answer="確認できた範囲で回答します。"),
    )

    assert result.status == "insufficient"
    assert "内部記事検索を完了できませんでした" not in result.missing_aspects
    assert "外部検索を完了できませんでした" not in result.missing_aspects
    assert result.missing_aspects.count(_INCOMPLETE_TASK_MISSING) == 1


def test_internal_hits_without_external_hits_can_answer() -> None:
    """外部ヒットが無くても内部ヒットがあればincompleteにならず、

    期間失敗 missing も出ない。
    """
    tasks = [_task("直近の外部発表を確認する")]
    evidence = [_internal_evidence()]
    outcome = _outcome(
        task_reports=[
            _report(
                task_index=0,
                research_goal=tasks[0].research_goal,
                internal_collection="succeeded",
                external_collection="succeeded",
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

    assert result.status == "answered"
    assert result.missing_aspects == []


def test_report_missing_deduplicates_against_review_missing() -> None:
    """missingはRun単位のreview_missingから1本だけ流れ、根拠取得の不足と

    重複排除される(この task は provider_failed でヒットゼロだが、外部収集の
    失敗であり internal_hit_count も 0 のため incomplete条件(0/0)にも
    当たる)。
    """
    tasks = [_task("既存のexternal task")]
    shared_text = "共有される不足理由"
    retrieval_missing = "回答に使える根拠を取得できませんでした"
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
        review_missing=[shared_text, retrieval_missing],
    )

    result = _assemble(
        plan=_search_plan(
            research_tasks=_research_tasks_from(tasks),
            target_time_window=None,
        ),
        outcome=outcome,
        evidence=[],
        answer_outcome=_draft(answer="確認できた範囲で回答します。"),
    )

    assert result.missing_aspects.count(_INCOMPLETE_TASK_MISSING) == 1
    assert _without_incomplete_phrase(result.missing_aspects) == [
        "回答に使える根拠を取得できませんでした",
        shared_text,
    ]
    assert result.missing_aspects.count(retrieval_missing) == 1


def test_generated_answer_preserves_missing_information_in_order() -> None:
    """機構由来のmissing_aspects(evidence空/incomplete task/

    review_missing)と根拠取得の不足が併存し、現行どおりの順序で並ぶ。
    """
    tasks = [_task("直近の外部発表を確認する")]
    outcome = _outcome(
        task_reports=[
            _zero_hit_report(
                task_index=0,
                research_goal=tasks[0].research_goal,
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
        answer_outcome=_draft(answer="確認できた範囲で回答します。"),
    )

    assert result.status == "insufficient"
    assert result.missing_aspects.count(_INCOMPLETE_TASK_MISSING) == 1
    assert _without_incomplete_phrase(result.missing_aspects) == [
        "回答に使える根拠を取得できませんでした",
        "reviewerが申告した不足",
    ]


def test_review_missing_is_deduplicated_against_mechanism_missing() -> None:
    """根拠取得の不足が機構由来のmissingと偶然一致しても重複排除される。"""
    retrieval_missing = "回答に使える根拠を取得できませんでした"
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
        review_missing=[retrieval_missing],
    )

    result = _assemble(
        plan=_search_plan(
            research_tasks=_research_tasks_from(tasks),
            target_time_window=None,
        ),
        outcome=outcome,
        evidence=[],
        answer_outcome=_draft(answer="確認できた範囲で回答します。"),
    )

    assert result.missing_aspects.count(retrieval_missing) == 1
