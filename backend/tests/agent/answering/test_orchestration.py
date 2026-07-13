"""Question answering orchestrator tests."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.agent.answering.contract import AnsweringRequest
from app.agent.answering.direct_answer.contract import DirectAnswerDraft
from app.agent.answering.evidence_answer.contract import (
    EvidenceAnswerDraft,
    EvidenceAnswerDraftInvalidError,
)
from app.agent.answering.orchestration import QuestionAnsweringOrchestrator
from app.agent.contract import AnswerQuestionInput, ExternalUrlSource
from app.agent.evidence_collection import EvidenceCollectionOutcome
from app.agent.evidence_collection.external_search import (
    ExternalSearchEvidence,
    ExternalSearchOutcome,
    ResearchTaskReport,
)
from app.agent.evidence_collection.internal_search.article_search import (
    InternalArticleContent,
    InternalArticleSearchHit,
)
from app.agent.planning.contract import (
    ExternalResearchTask,
    ExternalSearchPlan,
    InternalAndExternalPlan,
    InternalRetrievalPlan,
    NoRetrievalPlan,
    PlanningRequest,
    QuestionPlan,
    RetrievalPlan,
)
from app.agent.question_context.contract import AnswerRequirement, QuestionContext
from app.analysis.analyzed_article import InScopeAnalyzedArticle
from app.analysis.assessment.domain.result import InScope, InScopeCategory


def _as_of() -> datetime:
    return datetime(2026, 7, 7, 9, 0, tzinfo=UTC)


def _input(
    question: str = "NVIDIA の直近発表は投資判断に重要？",
    *,
    content_requirements: list[str] | None = None,
    response_requirements: list[str] | None = None,
    relevant_prior_coverage: str = "",
    active_goal: str = "",
    previous_answer: str = "",
) -> AnswerQuestionInput:
    return AnswerQuestionInput(
        context=QuestionContext(
            standalone_question=question,
            content_requirements=[
                AnswerRequirement(requirement_id=f"c{index}", description=description)
                for index, description in enumerate(content_requirements or [], start=1)
            ],
            response_requirements=[
                AnswerRequirement(requirement_id=f"p{index}", description=description)
                for index, description in enumerate(
                    response_requirements or [], start=1
                )
            ],
            relevant_prior_coverage=relevant_prior_coverage,
            active_goal=active_goal,
        ),
        as_of=_as_of(),
        previous_answer=previous_answer,
    )


def _internal_plan() -> InternalRetrievalPlan:
    return InternalRetrievalPlan(
        internal_queries=["NVIDIA AI GPU"],
        reason="internal evidence required",
    )


def _external_plan() -> ExternalSearchPlan:
    return ExternalSearchPlan(
        external_research_tasks=[_task(0)],
        target_time_window="今日",
        reason="external evidence required",
    )


def _mixed_plan() -> InternalAndExternalPlan:
    return InternalAndExternalPlan(
        internal_queries=["NVIDIA AI GPU"],
        external_research_tasks=[_task(0), _task(1)],
        target_time_window="直近24時間",
        reason="both evidence types required",
    )


def _task(index: int, goal: str | None = None) -> ExternalResearchTask:
    return ExternalResearchTask(collection_goal=goal or f"外部根拠 {index} を確認する")


def _internal_hit(
    *,
    assessment_id: int,
    title: str,
    summary: str | None = None,
) -> InternalArticleSearchHit:
    article = InScopeAnalyzedArticle(
        curation_id=assessment_id - 1000,
        title=title,
        summary=summary or f"{title} summary",
        assessment_result=InScope(
            category=InScopeCategory.AI,
            investor_take="投資家視点",
            key_points=[],
        ),
    )
    return InternalArticleSearchHit(
        assessment_id=assessment_id,
        article=article,
        content=InternalArticleContent.from_article(article, published_at=None),
        distance=0.1,
    )


def _external_evidence(
    *,
    task_index: int,
    url: str,
    title: str,
    claim: str,
) -> ExternalSearchEvidence:
    return ExternalSearchEvidence(
        source_ref=f"external-{task_index}-0",
        task_index=task_index,
        claim=claim,
        why_selected="selector explanation",
        url=url,
        title=title,
    )


def _report(
    *,
    task_index: int,
    missing: list[str] | None = None,
    evidence_count: int = 0,
) -> ResearchTaskReport:
    return ResearchTaskReport(
        task_index=task_index,
        collection_goal=f"外部根拠 {task_index} を確認する",
        status="succeeded",
        evidence_count=evidence_count,
        missing=missing or [],
    )


def _external_outcome(
    evidence: list[ExternalSearchEvidence],
    *,
    reports: list[ResearchTaskReport] | None = None,
    tasks: list[ExternalResearchTask] | None = None,
) -> ExternalSearchOutcome:
    tasks = tasks or [_task(0)]
    if reports is None:
        reports = [
            _report(
                task_index=index,
                evidence_count=sum(1 for item in evidence if item.task_index == index),
            )
            for index in range(len(tasks))
        ]
    return ExternalSearchOutcome(
        tasks=tasks,
        evidence=evidence,
        task_reports=reports,
        effective_agent_count=len(tasks),
    )


def _internal_outcome(count: int = 2) -> EvidenceCollectionOutcome:
    return EvidenceCollectionOutcome(
        internal_hits=[
            _internal_hit(assessment_id=1000 + index, title=f"internal {index}")
            for index in range(1, count + 1)
        ]
    )


def _external_outcome_only() -> EvidenceCollectionOutcome:
    evidence = [
        _external_evidence(
            task_index=0,
            url="https://example.com/external-1",
            title="external 1",
            claim="external claim",
        )
    ]
    return EvidenceCollectionOutcome(external_search=_external_outcome(evidence))


def _mixed_outcome() -> EvidenceCollectionOutcome:
    return EvidenceCollectionOutcome(
        internal_hits=[_internal_hit(assessment_id=1001, title="internal 1")],
        external_search=_external_outcome(
            [
                _external_evidence(
                    task_index=0,
                    url="https://example.com/external-1",
                    title="external 1",
                    claim="external claim",
                )
            ],
            tasks=[_task(0)],
        ),
    )


class FakePlanner:
    def __init__(self, plan: QuestionPlan | Exception) -> None:
        self._plan = plan
        self.calls: list[PlanningRequest] = []

    async def plan(self, request: PlanningRequest) -> QuestionPlan:
        self.calls.append(request)
        if isinstance(self._plan, Exception):
            raise self._plan
        return self._plan


class FakeEvidenceCollector:
    def __init__(self, outcome: EvidenceCollectionOutcome | Exception) -> None:
        self._outcome = outcome
        self.calls: list[tuple[RetrievalPlan, datetime]] = []

    async def collect(
        self,
        plan: RetrievalPlan,
        *,
        as_of: datetime,
    ) -> EvidenceCollectionOutcome:
        self.calls.append((plan, as_of))
        if isinstance(self._outcome, Exception):
            raise self._outcome
        return self._outcome


class FakeEvidenceAnswerer:
    def __init__(self, draft: EvidenceAnswerDraft | Exception) -> None:
        self._draft = draft
        self.calls: list[dict[str, object]] = []

    async def answer(
        self,
        *,
        request: AnsweringRequest,
        evidence: list[object],
        target_time_window: str | None,
    ) -> EvidenceAnswerDraft:
        self.calls.append(
            {
                "request": request,
                "evidence": evidence,
                "target_time_window": target_time_window,
            }
        )
        if isinstance(self._draft, Exception):
            raise self._draft
        return self._draft


class FakeDirectAnswerer:
    def __init__(self, draft: DirectAnswerDraft | Exception) -> None:
        self._draft = draft
        self.calls: list[dict[str, object]] = []

    async def answer(
        self,
        *,
        request: AnsweringRequest,
        previous_answer: str = "",
    ) -> DirectAnswerDraft:
        self.calls.append(
            {
                "request": request,
                "previous_answer": previous_answer,
            }
        )
        if isinstance(self._draft, Exception):
            raise self._draft
        return self._draft


class FakeProgressReporter:
    def __init__(self) -> None:
        self.stages: list[str] = []

    async def stage_changed(self, stage: str) -> None:
        self.stages.append(stage)


def _orchestrator(
    *,
    plan: QuestionPlan | Exception,
    outcome: EvidenceCollectionOutcome | Exception = AssertionError(
        "evidence_collector must not be called"
    ),
    draft: EvidenceAnswerDraft | Exception = AssertionError(
        "evidence_answerer must not be called"
    ),
    direct_draft: DirectAnswerDraft | Exception = AssertionError(
        "direct answerer must not be called"
    ),
    progress: FakeProgressReporter | None = None,
) -> tuple[
    QuestionAnsweringOrchestrator,
    FakePlanner,
    FakeEvidenceCollector,
    FakeEvidenceAnswerer,
    FakeDirectAnswerer,
]:
    planner = FakePlanner(plan)
    evidence_collector = FakeEvidenceCollector(outcome)
    evidence_answerer = FakeEvidenceAnswerer(draft)
    direct_answerer = FakeDirectAnswerer(direct_draft)
    kwargs = {}
    if progress is not None:
        kwargs["progress"] = progress
    orchestrator = QuestionAnsweringOrchestrator(
        planner=planner,
        evidence_collector=evidence_collector,
        evidence_answerer=evidence_answerer,
        direct_answerer=direct_answerer,
        **kwargs,
    )
    return orchestrator, planner, evidence_collector, evidence_answerer, direct_answerer


@pytest.mark.asyncio
async def test_answer_direct_plan_calls_direct_answerer_only() -> None:
    input_ = _input(
        "前回の結論だけ",
        content_requirements=["結論を説明する"],
        response_requirements=["結論だけを短く"],
        relevant_prior_coverage="前回は根拠を説明済み",
        active_goal="投資判断を調査中",
        previous_answer="根拠付き前回答 [[1]]",
    )
    direct_draft = DirectAnswerDraft(answer="こんにちは。何を確認しますか？")
    orchestrator, _, evidence_collector, evidence_answerer, direct_answerer = (
        _orchestrator(
            plan=NoRetrievalPlan(reason="direct answer"),
            direct_draft=direct_draft,
        )
    )

    result = await orchestrator.answer(input_)

    assert result.status == "answered"
    assert result.answer == direct_draft.answer
    assert result.sources == []
    assert result.missing_aspects == []
    assert result.retrieval.planned_mode == "none"
    assert result.retrieval.collection_failures == []
    assert not hasattr(result, "execution")
    assert direct_answerer.calls == [
        {
            "request": AnsweringRequest(context=input_.context, as_of=input_.as_of),
            "previous_answer": input_.previous_answer,
        }
    ]
    assert direct_answerer.calls[0]["request"].context is input_.context
    assert evidence_collector.calls == []
    assert evidence_answerer.calls == []


@pytest.mark.asyncio
async def test_answer_direct_plan_reports_planning_then_synthesizing() -> None:
    progress = FakeProgressReporter()
    orchestrator, _, _, _, _ = _orchestrator(
        plan=NoRetrievalPlan(reason="direct answer"),
        direct_draft=DirectAnswerDraft(answer="直接回答です。"),
        progress=progress,
    )

    await orchestrator.answer(_input("こんにちは"))

    assert progress.stages == ["planning", "synthesizing"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("plan", "outcome", "cited_refs"),
    [
        (_internal_plan(), _internal_outcome(1), ["1"]),
        (_external_plan(), _external_outcome_only(), ["1"]),
        (_mixed_plan(), _mixed_outcome(), ["1", "2"]),
    ],
)
async def test_answer_retrieval_plan_variants_do_not_call_direct_answerer(
    plan: RetrievalPlan,
    outcome: EvidenceCollectionOutcome,
    cited_refs: list[str],
) -> None:
    orchestrator, _, _, _, direct_answerer = _orchestrator(
        plan=plan,
        outcome=outcome,
        draft=EvidenceAnswerDraft(
            sufficiency="answered",
            answer="根拠から確認できます。",
            cited_refs=cited_refs,
        ),
    )

    result = await orchestrator.answer(_input())

    assert result.status == "answered"
    assert direct_answerer.calls == []


@pytest.mark.asyncio
async def test_answer_evidence_plan_reports_all_progress_stages_in_order() -> None:
    progress = FakeProgressReporter()
    orchestrator, _, _, _, _ = _orchestrator(
        plan=_mixed_plan(),
        outcome=_mixed_outcome(),
        draft=EvidenceAnswerDraft(
            sufficiency="answered",
            answer="根拠から確認できます。",
            cited_refs=["1", "2"],
        ),
        progress=progress,
    )

    await orchestrator.answer(_input())

    assert progress.stages == ["planning", "retrieving", "synthesizing"]


@pytest.mark.asyncio
async def test_answer_internal_sources_and_status_from_citations() -> None:
    orchestrator, _, _, _, _ = _orchestrator(
        plan=_internal_plan(),
        outcome=_internal_outcome(2),
        draft=EvidenceAnswerDraft(
            sufficiency="answered",
            answer="内部記事 1 と 2 から確認できます。",
            cited_refs=["1", "2"],
        ),
    )

    result = await orchestrator.answer(_input())

    assert result.status == "answered"
    assert result.retrieval.planned_mode == "internal"
    assert [source.source_ref for source in result.sources] == ["1", "2"]
    assert [source.title for source in result.sources] == ["internal 1", "internal 2"]
    assert result.missing_aspects == []


@pytest.mark.asyncio
async def test_answered_evidence_draft_with_no_unfulfilled_ids_stays_answered() -> None:
    answer = "内部根拠から確認できます。[[1]]"
    orchestrator, _, _, _, _ = _orchestrator(
        plan=_internal_plan(),
        outcome=_internal_outcome(1),
        draft=EvidenceAnswerDraft(
            sufficiency="answered",
            answer=answer,
            cited_refs=["1"],
            unfulfilled_requirement_ids=[],
        ),
    )

    result = await orchestrator.answer(
        _input(content_requirements=["投資判断への影響を説明する"])
    )

    assert (
        result.status,
        result.answer,
        [source.source_ref for source in result.sources],
        result.missing_aspects,
    ) == ("answered", answer, ["1"], [])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    (
        "content_requirements",
        "response_requirements",
        "unfulfilled_requirement_id",
        "description",
    ),
    [
        (["投資判断への影響を説明する"], [], "c1", "投資判断への影響を説明する"),
        ([], ["初心者向けに説明する"], "p1", "初心者向けに説明する"),
    ],
    ids=["content", "response"],
)
async def test_unfulfilled_requirement_caps_status_and_preserves_answer_sources(
    content_requirements: list[str],
    response_requirements: list[str],
    unfulfilled_requirement_id: str,
    description: str,
) -> None:
    answer = "確認できた範囲を回答します。[[1]]"
    orchestrator, _, _, _, _ = _orchestrator(
        plan=_internal_plan(),
        outcome=_internal_outcome(1),
        draft=EvidenceAnswerDraft(
            sufficiency="answered",
            answer=answer,
            cited_refs=["1"],
            unfulfilled_requirement_ids=[unfulfilled_requirement_id],
        ),
    )

    result = await orchestrator.answer(
        _input(
            content_requirements=content_requirements,
            response_requirements=response_requirements,
        )
    )

    assert (
        result.status,
        result.answer,
        [source.source_ref for source in result.sources],
        result.missing_aspects,
    ) == (
        "insufficient",
        answer,
        ["1"],
        [f"回答要望を満たせませんでした: {description}"],
    )


@pytest.mark.asyncio
async def test_requirement_missing_follows_existing_missing_in_context_order() -> None:
    tasks = [_task(0), _task(1)]
    outcome = EvidenceCollectionOutcome(
        external_search=_external_outcome(
            [],
            reports=[
                _report(task_index=1, missing=["外部タスク1の不足"]),
                _report(task_index=0, missing=["外部タスク0の不足"]),
            ],
            tasks=tasks,
        ),
        collection_failures=["internal_search"],
    )
    orchestrator, _, _, _, _ = _orchestrator(
        plan=_mixed_plan(),
        outcome=outcome,
        draft=EvidenceAnswerDraft(
            sufficiency="insufficient",
            answer="取得できた範囲では断定できません。",
            cited_refs=[],
            missing_aspects=["draftの不足"],
            unfulfilled_requirement_ids=["p2", "c2", "p1", "c1"],
        ),
    )

    result = await orchestrator.answer(
        _input(
            content_requirements=["content 1", "content 2"],
            response_requirements=["response 1", "response 2"],
        )
    )

    assert result.missing_aspects == [
        "回答に使える根拠を取得できませんでした",
        "内部記事検索を完了できませんでした",
        "外部タスク0の不足",
        "外部タスク1の不足",
        "draftの不足",
        "回答要望を満たせませんでした: content 1",
        "回答要望を満たせませんでした: content 2",
        "回答要望を満たせませんでした: response 1",
        "回答要望を満たせませんでした: response 2",
    ]


@pytest.mark.asyncio
async def test_requirement_missing_is_deduplicated_against_existing_missing() -> None:
    duplicate = "回答要望を満たせませんでした: 重複する要望"
    orchestrator, _, _, _, _ = _orchestrator(
        plan=_internal_plan(),
        outcome=_internal_outcome(1),
        draft=EvidenceAnswerDraft(
            sufficiency="insufficient",
            answer="一部のみ回答します。[[1]]",
            cited_refs=["1"],
            missing_aspects=[duplicate],
            unfulfilled_requirement_ids=["c2", "c1"],
        ),
    )

    result = await orchestrator.answer(
        _input(content_requirements=["重複する要望", "追加の要望"])
    )

    assert result.missing_aspects == [
        duplicate,
        "回答要望を満たせませんでした: 追加の要望",
    ]


@pytest.mark.asyncio
async def test_answer_rejects_unknown_unfulfilled_requirement_id() -> None:
    orchestrator, _, _, _, _ = _orchestrator(
        plan=_internal_plan(),
        outcome=_internal_outcome(1),
        draft=EvidenceAnswerDraft(
            sufficiency="answered",
            answer="内部根拠から確認できます。[[1]]",
            cited_refs=["1"],
            unfulfilled_requirement_ids=["unknown"],
        ),
    )

    with pytest.raises(EvidenceAnswerDraftInvalidError):
        await orchestrator.answer(
            _input(content_requirements=["投資判断への影響を説明する"])
        )


@pytest.mark.asyncio
async def test_answer_external_source_is_cited_source_only() -> None:
    orchestrator, _, _, _, _ = _orchestrator(
        plan=_external_plan(),
        outcome=_external_outcome_only(),
        draft=EvidenceAnswerDraft(
            sufficiency="answered",
            answer="外部根拠から確認できます。",
            cited_refs=["1"],
        ),
    )

    result = await orchestrator.answer(_input())

    assert result.status == "answered"
    assert result.retrieval.planned_mode == "external"
    assert len(result.sources) == 1
    assert isinstance(result.sources[0], ExternalUrlSource)


@pytest.mark.asyncio
async def test_answer_mixed_plan_with_both_evidence_types_cited() -> None:
    orchestrator, _, _, _, _ = _orchestrator(
        plan=_mixed_plan(),
        outcome=_mixed_outcome(),
        draft=EvidenceAnswerDraft(
            sufficiency="answered",
            answer="内部根拠と外部根拠から確認できます。",
            cited_refs=["1", "2"],
        ),
    )

    result = await orchestrator.answer(_input())

    assert result.status == "answered"
    assert result.retrieval.planned_mode == "internal_and_external"
    assert [source.source_ref for source in result.sources] == ["1", "2"]


@pytest.mark.asyncio
async def test_answer_mixed_plan_omits_unused_external_source() -> None:
    orchestrator, _, _, _, _ = _orchestrator(
        plan=_mixed_plan(),
        outcome=_mixed_outcome(),
        draft=EvidenceAnswerDraft(
            sufficiency="answered",
            answer="内部根拠だけで確認できます。",
            cited_refs=["1"],
        ),
    )

    result = await orchestrator.answer(_input())

    assert result.status == "answered"
    assert result.retrieval.planned_mode == "internal_and_external"
    assert [source.source_ref for source in result.sources] == ["1"]
    assert all(not isinstance(source, ExternalUrlSource) for source in result.sources)


@pytest.mark.asyncio
async def test_answer_empty_retrieval_evidence_calls_synthesis() -> None:
    draft = EvidenceAnswerDraft(
        sufficiency="insufficient",
        answer=(
            "検索で引用できる根拠は見つかりませんでした。"
            "一般論としては参考程度に扱ってください。"
        ),
        cited_refs=[],
        missing_aspects=["引用できる検索根拠"],
    )
    orchestrator, _, _, evidence_answerer, _ = _orchestrator(
        plan=_internal_plan(),
        outcome=EvidenceCollectionOutcome(),
        draft=draft,
    )

    result = await orchestrator.answer(_input())

    assert result.status == "insufficient"
    assert result.answer == draft.answer
    assert result.sources == []
    assert result.missing_aspects
    assert "引用できる検索根拠" in result.missing_aspects
    assert len(evidence_answerer.calls) == 1
    assert evidence_answerer.calls[0]["evidence"] == []


@pytest.mark.asyncio
async def test_answer_collection_failures_cap_answered_draft_to_insufficient() -> None:
    orchestrator, _, _, _, _ = _orchestrator(
        plan=_mixed_plan(),
        outcome=EvidenceCollectionOutcome(
            internal_hits=[_internal_hit(assessment_id=1001, title="internal 1")],
            collection_failures=["external_search"],
        ),
        draft=EvidenceAnswerDraft(
            sufficiency="answered",
            answer="内部根拠の範囲では確認できます。",
            cited_refs=["1"],
        ),
    )

    result = await orchestrator.answer(_input())

    assert result.status == "insufficient"
    assert result.answer == "内部根拠の範囲では確認できます。"
    assert result.retrieval.collection_failures == ["external_search"]
    assert any("外部" in item for item in result.missing_aspects)


@pytest.mark.asyncio
async def test_answer_adopts_insufficient_draft_with_partial_citations() -> None:
    orchestrator, _, _, _, _ = _orchestrator(
        plan=_internal_plan(),
        outcome=_internal_outcome(1),
        draft=EvidenceAnswerDraft(
            sufficiency="insufficient",
            answer="内部根拠では断定できません。[[1]]",
            cited_refs=["1"],
            missing_aspects=["会社側の一次情報"],
        ),
    )

    result = await orchestrator.answer(_input())

    assert result.status == "insufficient"
    assert result.answer == "内部根拠では断定できません。[[1]]"
    assert [source.source_ref for source in result.sources] == ["1"]
    assert result.missing_aspects == ["会社側の一次情報"]


@pytest.mark.asyncio
async def test_answer_missing_aspects_are_ordered_and_deduplicated() -> None:
    tasks = [_task(0), _task(1)]
    reports = [
        _report(task_index=1, missing=["市場予想値", "会社側コメント"]),
        _report(task_index=0, missing=["市場予想値", "実績値"], evidence_count=1),
    ]
    orchestrator, _, _, _, _ = _orchestrator(
        plan=_mixed_plan(),
        outcome=EvidenceCollectionOutcome(
            external_search=_external_outcome(
                [
                    _external_evidence(
                        task_index=0,
                        url="https://example.com/external-1",
                        title="external 1",
                        claim="external claim",
                    )
                ],
                reports=reports,
                tasks=tasks,
            ),
            collection_failures=["internal_search"],
        ),
        draft=EvidenceAnswerDraft(
            sufficiency="insufficient",
            answer="根拠が不足しています。",
            cited_refs=["1"],
            missing_aspects=["会社側コメント", "経営陣の見通し"],
        ),
    )

    result = await orchestrator.answer(_input())

    assert "内部" in result.missing_aspects[0]
    assert result.missing_aspects[1:] == [
        "市場予想値",
        "実績値",
        "会社側コメント",
        "経営陣の見通し",
    ]


@pytest.mark.asyncio
async def test_answer_rejects_unknown_citation_ref() -> None:
    orchestrator, _, _, _, _ = _orchestrator(
        plan=_internal_plan(),
        outcome=_internal_outcome(1),
        draft=EvidenceAnswerDraft(
            sufficiency="answered",
            answer="存在しない根拠を引用しています。",
            cited_refs=["2"],
        ),
    )

    with pytest.raises(EvidenceAnswerDraftInvalidError, match="unknown citation ref"):
        await orchestrator.answer(_input())


@pytest.mark.asyncio
async def test_answer_deduplicates_repeated_citation_refs_in_source_order() -> None:
    orchestrator, _, _, _, _ = _orchestrator(
        plan=_internal_plan(),
        outcome=_internal_outcome(2),
        draft=EvidenceAnswerDraft(
            sufficiency="answered",
            answer="重複引用を含みます。",
            cited_refs=["2", "1", "2", "1"],
        ),
    )

    result = await orchestrator.answer(_input())

    assert [source.source_ref for source in result.sources] == ["1", "2"]


@pytest.mark.asyncio
async def test_answer_passes_pipeline_inputs_and_variant_time_window() -> None:
    input_ = _input(
        content_requirements=["発表後の差分を説明する"],
        response_requirements=["詳しく説明する"],
        relevant_prior_coverage="発表内容は既出",
        active_goal="投資判断を調査中",
    )
    orchestrator, planner, evidence_collector, evidence_answerer, _ = _orchestrator(
        plan=_mixed_plan(),
        outcome=_mixed_outcome(),
        draft=EvidenceAnswerDraft(
            sufficiency="answered",
            answer="確認できます。",
            cited_refs=["1", "2"],
        ),
    )

    await orchestrator.answer(input_)

    assert planner.calls == [
        PlanningRequest(context=input_.context, as_of=input_.as_of)
    ]
    assert planner.calls[0].context is input_.context
    assert evidence_collector.calls == [(_mixed_plan(), _as_of())]
    assert evidence_answerer.calls[0]["request"] == AnsweringRequest(
        context=input_.context,
        as_of=input_.as_of,
    )
    assert evidence_answerer.calls[0]["request"].context is input_.context
    assert evidence_answerer.calls[0]["target_time_window"] == "直近24時間"
    assert set(evidence_answerer.calls[0]) == {
        "request",
        "evidence",
        "target_time_window",
    }


@pytest.mark.asyncio
async def test_answer_passes_none_time_window_for_internal_plan() -> None:
    orchestrator, _, _, evidence_answerer, _ = _orchestrator(
        plan=_internal_plan(),
        outcome=_internal_outcome(1),
        draft=EvidenceAnswerDraft(
            sufficiency="answered",
            answer="確認できます。",
            cited_refs=["1"],
        ),
    )

    await orchestrator.answer(_input())

    assert evidence_answerer.calls[0]["target_time_window"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("plan", "outcome", "draft", "message"),
    [
        (
            RuntimeError("planner failed"),
            EvidenceCollectionOutcome(),
            EvidenceAnswerDraft(
                sufficiency="answered",
                answer="x",
                cited_refs=["1"],
            ),
            "planner failed",
        ),
        (
            _internal_plan(),
            RuntimeError("evidence_collector failed"),
            EvidenceAnswerDraft(
                sufficiency="answered",
                answer="x",
                cited_refs=["1"],
            ),
            "evidence_collector failed",
        ),
        (
            _internal_plan(),
            _internal_outcome(1),
            RuntimeError("evidence_answerer failed"),
            "evidence_answerer failed",
        ),
    ],
)
async def test_answer_propagates_step_exceptions(
    plan: QuestionPlan | Exception,
    outcome: EvidenceCollectionOutcome | Exception,
    draft: EvidenceAnswerDraft | Exception,
    message: str,
) -> None:
    orchestrator, _, _, _, _ = _orchestrator(plan=plan, outcome=outcome, draft=draft)

    with pytest.raises(RuntimeError, match=message):
        await orchestrator.answer(_input())


@pytest.mark.asyncio
async def test_answer_propagates_direct_answerer_exception() -> None:
    orchestrator, _, _, _, _ = _orchestrator(
        plan=NoRetrievalPlan(reason="direct answer"),
        direct_draft=RuntimeError("direct failed"),
    )

    with pytest.raises(RuntimeError, match="direct failed"):
        await orchestrator.answer(_input("こんにちは"))
