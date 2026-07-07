"""Question answer orchestration service tests."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from app.agent.answering.direct import DirectAnswerDraft
from app.agent.answering.retrieval import RetrievalOutcome
from app.agent.answering.service import QuestionAnsweringService
from app.agent.answering.synthesis import (
    AnswerDraft,
    AnswerDraftInvalidError,
)
from app.agent.contract import AnswerQuestionInput, ExternalUrlSource
from app.agent.external_search import (
    ExternalSearchEvidence,
    ExternalSearchOutcome,
    ResearchTaskReport,
)
from app.agent.internal_retrieval.article_search import (
    InternalArticleContent,
    InternalArticleSearchHit,
)
from app.agent.planning.contract import (
    ExternalResearchTask,
    ExternalSearchPlan,
    InternalAndExternalPlan,
    InternalRetrievalPlan,
    NoRetrievalPlan,
    QuestionPlan,
    RetrievalPlan,
)
from app.analysis.analyzed_article import InScopeAnalyzedArticle
from app.analysis.assessment.domain.result import InScope, InScopeCategory


def _as_of() -> datetime:
    return datetime(2026, 7, 7, 9, 0, tzinfo=UTC)


def _input(
    question: str = "NVIDIA の直近発表は投資判断に重要？",
) -> AnswerQuestionInput:
    return AnswerQuestionInput(question=question, as_of=_as_of())


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


def _internal_outcome(count: int = 2) -> RetrievalOutcome:
    return RetrievalOutcome(
        internal_hits=[
            _internal_hit(assessment_id=1000 + index, title=f"internal {index}")
            for index in range(1, count + 1)
        ]
    )


def _external_outcome_only() -> RetrievalOutcome:
    evidence = [
        _external_evidence(
            task_index=0,
            url="https://example.com/external-1",
            title="external 1",
            claim="external claim",
        )
    ]
    return RetrievalOutcome(external_search=_external_outcome(evidence))


def _mixed_outcome() -> RetrievalOutcome:
    return RetrievalOutcome(
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
        self.calls: list[AnswerQuestionInput] = []

    async def plan(self, input: AnswerQuestionInput) -> QuestionPlan:
        self.calls.append(input)
        if isinstance(self._plan, Exception):
            raise self._plan
        return self._plan


class FakeRetriever:
    def __init__(self, outcome: RetrievalOutcome | Exception) -> None:
        self._outcome = outcome
        self.calls: list[tuple[RetrievalPlan, datetime]] = []

    async def retrieve(
        self,
        plan: RetrievalPlan,
        *,
        as_of: datetime,
    ) -> RetrievalOutcome:
        self.calls.append((plan, as_of))
        if isinstance(self._outcome, Exception):
            raise self._outcome
        return self._outcome


class FakeSynthesizer:
    def __init__(self, draft: AnswerDraft | Exception) -> None:
        self._draft = draft
        self.calls: list[dict[str, object]] = []

    async def synthesize(
        self,
        *,
        question: str,
        evidence: list[object],
        as_of: datetime,
        target_time_window: str | None,
    ) -> AnswerDraft:
        self.calls.append(
            {
                "question": question,
                "evidence": evidence,
                "as_of": as_of,
                "target_time_window": target_time_window,
            }
        )
        if isinstance(self._draft, Exception):
            raise self._draft
        return self._draft


class FakeDirectAnswerer:
    def __init__(self, draft: DirectAnswerDraft | Exception) -> None:
        self._draft = draft
        self.calls: list[tuple[str, datetime]] = []

    async def answer(
        self,
        *,
        question: str,
        as_of: datetime,
    ) -> DirectAnswerDraft:
        self.calls.append((question, as_of))
        if isinstance(self._draft, Exception):
            raise self._draft
        return self._draft


def _service(
    *,
    plan: QuestionPlan | Exception,
    outcome: RetrievalOutcome | Exception = AssertionError(
        "retriever must not be called"
    ),
    draft: AnswerDraft | Exception = AssertionError("synthesizer must not be called"),
    direct_draft: DirectAnswerDraft | Exception = AssertionError(
        "direct answerer must not be called"
    ),
) -> tuple[
    QuestionAnsweringService,
    FakePlanner,
    FakeRetriever,
    FakeSynthesizer,
    FakeDirectAnswerer,
]:
    planner = FakePlanner(plan)
    retriever = FakeRetriever(outcome)
    synthesizer = FakeSynthesizer(draft)
    direct_answerer = FakeDirectAnswerer(direct_draft)
    service = QuestionAnsweringService(
        planner=planner,
        retriever=retriever,
        synthesizer=synthesizer,
        direct_answerer=direct_answerer,
    )
    return service, planner, retriever, synthesizer, direct_answerer


@pytest.mark.asyncio
async def test_answer_direct_plan_calls_direct_answerer_only() -> None:
    input_ = _input("こんにちは")
    direct_draft = DirectAnswerDraft(answer="こんにちは。何を確認しますか？")
    service, _, retriever, synthesizer, direct_answerer = _service(
        plan=NoRetrievalPlan(reason="direct answer"),
        direct_draft=direct_draft,
    )

    result = await service.answer(input_)

    assert result.status == "answered"
    assert result.answer == direct_draft.answer
    assert result.sources == []
    assert result.missing_aspects == []
    assert result.retrieval.planned_mode == "none"
    assert result.retrieval.unmet_requirements == []
    assert not hasattr(result, "execution")
    assert direct_answerer.calls == [(input_.question, input_.as_of)]
    assert retriever.calls == []
    assert synthesizer.calls == []


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
    outcome: RetrievalOutcome,
    cited_refs: list[str],
) -> None:
    service, _, _, _, direct_answerer = _service(
        plan=plan,
        outcome=outcome,
        draft=AnswerDraft(
            sufficiency="answered",
            answer="根拠から確認できます。",
            cited_refs=cited_refs,
        ),
    )

    result = await service.answer(_input())

    assert result.status == "answered"
    assert direct_answerer.calls == []


@pytest.mark.asyncio
async def test_answer_internal_sources_and_status_from_citations() -> None:
    service, _, _, _, _ = _service(
        plan=_internal_plan(),
        outcome=_internal_outcome(2),
        draft=AnswerDraft(
            sufficiency="answered",
            answer="内部記事 1 と 2 から確認できます。",
            cited_refs=["1", "2"],
        ),
    )

    result = await service.answer(_input())

    assert result.status == "answered"
    assert result.retrieval.planned_mode == "internal"
    assert [source.source_ref for source in result.sources] == ["1", "2"]
    assert [source.title for source in result.sources] == ["internal 1", "internal 2"]
    assert result.missing_aspects == []


@pytest.mark.asyncio
async def test_answer_external_source_is_cited_source_only() -> None:
    service, _, _, _, _ = _service(
        plan=_external_plan(),
        outcome=_external_outcome_only(),
        draft=AnswerDraft(
            sufficiency="answered",
            answer="外部根拠から確認できます。",
            cited_refs=["1"],
        ),
    )

    result = await service.answer(_input())

    assert result.status == "answered"
    assert result.retrieval.planned_mode == "external"
    assert len(result.sources) == 1
    assert isinstance(result.sources[0], ExternalUrlSource)


@pytest.mark.asyncio
async def test_answer_mixed_plan_with_both_evidence_types_cited() -> None:
    service, _, _, _, _ = _service(
        plan=_mixed_plan(),
        outcome=_mixed_outcome(),
        draft=AnswerDraft(
            sufficiency="answered",
            answer="内部根拠と外部根拠から確認できます。",
            cited_refs=["1", "2"],
        ),
    )

    result = await service.answer(_input())

    assert result.status == "answered"
    assert result.retrieval.planned_mode == "internal_and_external"
    assert [source.source_ref for source in result.sources] == ["1", "2"]


@pytest.mark.asyncio
async def test_answer_mixed_plan_omits_unused_external_source() -> None:
    service, _, _, _, _ = _service(
        plan=_mixed_plan(),
        outcome=_mixed_outcome(),
        draft=AnswerDraft(
            sufficiency="answered",
            answer="内部根拠だけで確認できます。",
            cited_refs=["1"],
        ),
    )

    result = await service.answer(_input())

    assert result.status == "answered"
    assert result.retrieval.planned_mode == "internal_and_external"
    assert [source.source_ref for source in result.sources] == ["1"]
    assert all(not isinstance(source, ExternalUrlSource) for source in result.sources)


@pytest.mark.asyncio
async def test_answer_empty_retrieval_evidence_skips_synthesis() -> None:
    service, _, _, synthesizer, _ = _service(
        plan=_internal_plan(),
        outcome=RetrievalOutcome(),
    )

    result = await service.answer(_input())

    assert result.status == "insufficient"
    assert result.sources == []
    assert result.missing_aspects
    assert "根拠" in result.missing_aspects[0]
    assert synthesizer.calls == []


@pytest.mark.asyncio
async def test_answer_unmet_requirements_cap_answered_draft_to_insufficient() -> None:
    service, _, _, _, _ = _service(
        plan=_mixed_plan(),
        outcome=RetrievalOutcome(
            internal_hits=[_internal_hit(assessment_id=1001, title="internal 1")],
            unmet_requirements=["external_search"],
        ),
        draft=AnswerDraft(
            sufficiency="answered",
            answer="内部根拠の範囲では確認できます。",
            cited_refs=["1"],
        ),
    )

    result = await service.answer(_input())

    assert result.status == "insufficient"
    assert result.answer == "内部根拠の範囲では確認できます。"
    assert result.retrieval.unmet_requirements == ["external_search"]
    assert any("外部" in item for item in result.missing_aspects)


@pytest.mark.asyncio
async def test_answer_adopts_insufficient_draft_with_partial_citations() -> None:
    service, _, _, _, _ = _service(
        plan=_internal_plan(),
        outcome=_internal_outcome(1),
        draft=AnswerDraft(
            sufficiency="insufficient",
            answer="内部根拠では断定できません。",
            cited_refs=["1"],
            missing_aspects=["会社側の一次情報"],
        ),
    )

    result = await service.answer(_input())

    assert result.status == "insufficient"
    assert [source.source_ref for source in result.sources] == ["1"]
    assert result.missing_aspects == ["会社側の一次情報"]


@pytest.mark.asyncio
async def test_answer_missing_aspects_are_ordered_and_deduplicated() -> None:
    tasks = [_task(0), _task(1)]
    reports = [
        _report(task_index=1, missing=["市場予想値", "会社側コメント"]),
        _report(task_index=0, missing=["市場予想値", "実績値"], evidence_count=1),
    ]
    service, _, _, _, _ = _service(
        plan=_mixed_plan(),
        outcome=RetrievalOutcome(
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
            unmet_requirements=["internal_retrieval"],
        ),
        draft=AnswerDraft(
            sufficiency="insufficient",
            answer="根拠が不足しています。",
            cited_refs=["1"],
            missing_aspects=["会社側コメント", "経営陣の見通し"],
        ),
    )

    result = await service.answer(_input())

    assert "内部" in result.missing_aspects[0]
    assert result.missing_aspects[1:] == [
        "市場予想値",
        "実績値",
        "会社側コメント",
        "経営陣の見通し",
    ]


@pytest.mark.asyncio
async def test_answer_rejects_unknown_citation_ref() -> None:
    service, _, _, _, _ = _service(
        plan=_internal_plan(),
        outcome=_internal_outcome(1),
        draft=AnswerDraft(
            sufficiency="answered",
            answer="存在しない根拠を引用しています。",
            cited_refs=["2"],
        ),
    )

    with pytest.raises(AnswerDraftInvalidError, match="unknown citation ref"):
        await service.answer(_input())


@pytest.mark.asyncio
async def test_answer_deduplicates_repeated_citation_refs_in_source_order() -> None:
    service, _, _, _, _ = _service(
        plan=_internal_plan(),
        outcome=_internal_outcome(2),
        draft=AnswerDraft(
            sufficiency="answered",
            answer="重複引用を含みます。",
            cited_refs=["2", "1", "2", "1"],
        ),
    )

    result = await service.answer(_input())

    assert [source.source_ref for source in result.sources] == ["1", "2"]


def test_answer_draft_rejects_answered_without_citations() -> None:
    with pytest.raises(ValidationError):
        AnswerDraft(
            sufficiency="answered",
            answer="根拠を引用せずに回答しています。",
        )


def test_answer_draft_rejects_answered_with_missing_aspects() -> None:
    with pytest.raises(ValidationError):
        AnswerDraft(
            sufficiency="answered",
            answer="回答できました。",
            cited_refs=["1"],
            missing_aspects=["不足"],
        )


def test_answer_draft_rejects_insufficient_without_missing_aspects() -> None:
    with pytest.raises(ValidationError):
        AnswerDraft(
            sufficiency="insufficient",
            answer="断定できません。",
        )


@pytest.mark.parametrize("answer", ["", "   ", "\n"])
def test_answer_draft_rejects_blank_answer(answer: str) -> None:
    with pytest.raises(ValidationError):
        AnswerDraft(
            sufficiency="insufficient",
            answer=answer,
            missing_aspects=["不足"],
        )


@pytest.mark.parametrize("missing", ["", "   ", "\n"])
def test_answer_draft_rejects_blank_missing_aspect(missing: str) -> None:
    with pytest.raises(ValidationError):
        AnswerDraft(
            sufficiency="insufficient",
            answer="断定できません。",
            missing_aspects=[missing],
        )


@pytest.mark.parametrize("answer", ["", "   ", "\n"])
def test_direct_answer_draft_rejects_blank_answer(answer: str) -> None:
    with pytest.raises(ValidationError):
        DirectAnswerDraft(answer=answer)


@pytest.mark.asyncio
async def test_answer_passes_pipeline_inputs_and_variant_time_window() -> None:
    input_ = _input()
    service, planner, retriever, synthesizer, _ = _service(
        plan=_mixed_plan(),
        outcome=_mixed_outcome(),
        draft=AnswerDraft(
            sufficiency="answered",
            answer="確認できます。",
            cited_refs=["1", "2"],
        ),
    )

    await service.answer(input_)

    assert planner.calls == [input_]
    assert retriever.calls == [(_mixed_plan(), _as_of())]
    assert synthesizer.calls[0]["question"] == input_.question
    assert synthesizer.calls[0]["as_of"] == input_.as_of
    assert synthesizer.calls[0]["target_time_window"] == "直近24時間"


@pytest.mark.asyncio
async def test_answer_passes_none_time_window_for_internal_plan() -> None:
    service, _, _, synthesizer, _ = _service(
        plan=_internal_plan(),
        outcome=_internal_outcome(1),
        draft=AnswerDraft(
            sufficiency="answered",
            answer="確認できます。",
            cited_refs=["1"],
        ),
    )

    await service.answer(_input())

    assert synthesizer.calls[0]["target_time_window"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("plan", "outcome", "draft", "message"),
    [
        (
            RuntimeError("planner failed"),
            RetrievalOutcome(),
            AnswerDraft(
                sufficiency="answered",
                answer="x",
                cited_refs=["1"],
            ),
            "planner failed",
        ),
        (
            _internal_plan(),
            RuntimeError("retriever failed"),
            AnswerDraft(
                sufficiency="answered",
                answer="x",
                cited_refs=["1"],
            ),
            "retriever failed",
        ),
        (
            _internal_plan(),
            _internal_outcome(1),
            RuntimeError("synthesizer failed"),
            "synthesizer failed",
        ),
    ],
)
async def test_answer_propagates_step_exceptions(
    plan: QuestionPlan | Exception,
    outcome: RetrievalOutcome | Exception,
    draft: AnswerDraft | Exception,
    message: str,
) -> None:
    service, _, _, _, _ = _service(plan=plan, outcome=outcome, draft=draft)

    with pytest.raises(RuntimeError, match=message):
        await service.answer(_input())


@pytest.mark.asyncio
async def test_answer_propagates_direct_answerer_exception() -> None:
    service, _, _, _, _ = _service(
        plan=NoRetrievalPlan(reason="direct answer"),
        direct_draft=RuntimeError("direct failed"),
    )

    with pytest.raises(RuntimeError, match="direct failed"):
        await service.answer(_input("こんにちは"))
