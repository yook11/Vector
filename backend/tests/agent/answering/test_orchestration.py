"""AnsweringRunner workflowとresult assemblyの回帰テスト。"""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pytest

from app.agent.answering.contract import AnsweringRequest
from app.agent.answering.direct_answer.contract import (
    DirectAnswerDraft,
    DirectAnswerInput,
)
from app.agent.answering.evidence_answer.contract import (
    EvidenceAnswerDraft,
    EvidenceAnswerDraftInvalidError,
    EvidenceAnswerInput,
)
from app.agent.contract import AnswerQuestionResult, ExternalUrlSource
from app.agent.evidence_collection import EvidenceCollectionService
from app.agent.evidence_collection.external_search import (
    ExternalQueryDraft,
    ExternalSearchHit,
    ExternalSearchService,
)
from app.agent.evidence_collection.internal_search import (
    InternalArticleContent,
    InternalArticleSearchHit,
)
from app.agent.evidence_collection.internal_search.query_embedding import (
    InternalSearchQueries,
)
from app.agent.evidence_review import (
    EvidenceReviewer,
    EvidenceReviewerDraft,
    ExternalSearchEvidence,
)
from app.agent.planning.contract import (
    DirectAnswerPlan,
    ExternalResearchTask,
    PlanningInput,
    QuestionPlan,
    ResearchTask,
    SearchPlan,
    TargetTimeWindow,
)
from app.agent.running import AnsweringPhases, AnsweringRunner, RunInput
from app.agent.runtime.contract import AgentResponseDefect, AgentResponseInvalidError
from app.agent.threads.contracts import ThreadMessageSnapshot
from app.analysis.analyzed_article import InScopeAnalyzedArticle
from app.analysis.assessment.domain.result import InScope, InScopeCategory
from tests.agent.running._harness import (
    ExternalScopes,
    PassThroughOrganizer,
    run_identity,
)


def _as_of() -> datetime:
    return datetime(2026, 7, 7, 9, 0, tzinfo=UTC)


def _draft(*, answer: str, cited_refs: list[str] | None = None) -> EvidenceAnswerDraft:
    """EvidenceAnswerDraftはanswerとcited_refsだけを持つ

    (sufficiency/missing_aspects/unfulfilled_requirement_idsは撤去済み)。
    """
    return EvidenceAnswerDraft(answer=answer, cited_refs=cited_refs or [])


@dataclass(frozen=True, slots=True)
class _WorkflowInput:
    question: str
    as_of: datetime
    previous_answer: str = ""


def _input(
    question: str = "NVIDIA の直近発表は投資判断に重要？",
    *,
    previous_answer: str = "",
) -> _WorkflowInput:
    return _WorkflowInput(
        question=question,
        as_of=_as_of(),
        previous_answer=previous_answer,
    )


def _direct_plan() -> DirectAnswerPlan:
    return DirectAnswerPlan()


def _search_plan(
    *,
    tasks: list[ExternalResearchTask] | None = None,
    target_time_window: TargetTimeWindow | None = None,
) -> SearchPlan:
    """各taskへ同一query("NVIDIA AI GPU")を配分する(query内容はこのtestでは非対象)。"""
    return SearchPlan(
        research_tasks=[
            ResearchTask(
                research_goal=task.research_goal,
                article_search_queries=["NVIDIA AI GPU"],
            )
            for task in (tasks or [_task(0)])
        ],
        target_time_window=target_time_window,
    )


def _task(index: int, goal: str | None = None) -> ExternalResearchTask:
    return ExternalResearchTask(research_goal=goal or f"外部根拠 {index} を確認する")


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
        option_index=task_index,
        task_index=task_index,
        claim=claim,
        why_selected="reviewer explanation",
        url=url,
        title=title,
    )


@dataclass(frozen=True, slots=True)
class _FixtureTaskMissing:
    """_external_runtime_for が reviewer draft の missing を組むための内部 fixture

    (production ResearchTaskReport ではない。D4-S2 で report shape が再設計
    されるため、このファイルは production 型を経由せず reviewer 応答の
    missing 伝搬だけを表現する)。
    """

    task_index: int
    missing: list[str] = field(default_factory=list)


def _report(
    *,
    task_index: int,
    missing: list[str] | None = None,
) -> _FixtureTaskMissing:
    return _FixtureTaskMissing(task_index=task_index, missing=missing or [])


@dataclass(frozen=True, slots=True)
class _FixtureExternalOutcome:
    """_external_runtime_for がtask別evidence/missingを引くための内部fixture。"""

    evidence: list[ExternalSearchEvidence] = field(default_factory=list)
    task_reports: list[_FixtureTaskMissing] = field(default_factory=list)


def _external_outcome(
    evidence: list[ExternalSearchEvidence],
    *,
    reports: list[_FixtureTaskMissing] | None = None,
    tasks: list[ExternalResearchTask] | None = None,
) -> _FixtureExternalOutcome:
    tasks = tasks or [_task(0)]
    if reports is None:
        reports = [_FixtureTaskMissing(task_index=index) for index in range(len(tasks))]
    return _FixtureExternalOutcome(evidence=evidence, task_reports=reports)


@dataclass(frozen=True, slots=True)
class _RetrievalFixture:
    """workflow harness用の意図記述DTO(production EvidenceCollectionOutcomeとは別物)。

    internal_hitsはreviewer精査前のraw hit(FakeInternalSearchが返す値)であり、
    production側のinternal_evidence(精査後、claim付き)とは型が違う。
    このfixtureのinternal_hits/external_searchは「reviewerが全選択肢を採用した
    場合に到達してほしい最終形」を表し、_external_runtime_forがそれに対応する
    reviewer draft(統合index空間で内部→外部の順に全採用)を組み立てる。
    D4-S2: run単位のcollection_failuresは廃止されたため、このfixtureも持たない
    (internal検索を失敗させたい場合は_orchestrator()のinternal_errorを使う)。
    """

    internal_hits: list[InternalArticleSearchHit] = field(default_factory=list)
    external_search: _FixtureExternalOutcome | None = None


def _internal_outcome(count: int = 2) -> _RetrievalFixture:
    return _RetrievalFixture(
        internal_hits=[
            _internal_hit(assessment_id=1000 + index, title=f"internal {index}")
            for index in range(1, count + 1)
        ],
        external_search=_external_outcome([]),
    )


def _external_outcome_only() -> _RetrievalFixture:
    evidence = [
        _external_evidence(
            task_index=0,
            url="https://example.com/external-1",
            title="external 1",
            claim="external claim",
        )
    ]
    return _RetrievalFixture(external_search=_external_outcome(evidence))


def _both_evidence_outcome() -> _RetrievalFixture:
    return _RetrievalFixture(
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
    def __init__(
        self,
        plan: QuestionPlan | Exception,
        *,
        timeline: CallTimeline | None = None,
    ) -> None:
        self._plan = plan
        self._timeline = timeline
        self.calls: list[PlanningInput] = []

    async def plan(self, input: PlanningInput) -> QuestionPlan:
        if self._timeline is not None:
            self._timeline.record("planner.plan")
        self.calls.append(input)
        if isinstance(self._plan, Exception):
            raise self._plan
        return self._plan


class FakeInternalSearch:
    def __init__(
        self,
        outcome: _RetrievalFixture | Exception,
        *,
        timeline: CallTimeline | None = None,
    ) -> None:
        self._outcome = outcome
        self._timeline = timeline
        self.calls: list[InternalSearchQueries] = []

    async def search(self, queries: Any) -> list[InternalArticleSearchHit]:
        if self._timeline is not None:
            self._timeline.record("internal_search.search_articles")
        self.calls.append(queries)
        if isinstance(self._outcome, Exception):
            raise self._outcome
        return self._outcome.internal_hits


class FakeExternalScopes:
    def __init__(
        self,
        scopes: ExternalScopes | None = None,
        *,
        timeline: CallTimeline | None = None,
    ) -> None:
        self._scopes = scopes
        self._timeline = timeline

    @asynccontextmanager
    async def external_search_scope(self):
        if self._timeline is not None:
            self._timeline.record("external_search.activate")
        if self._scopes is None:
            raise AssertionError("external search must not activate for this plan")
        yield self._scopes.external_search

    @asynccontextmanager
    async def reviewer_scope(self):
        if self._scopes is None:
            raise AssertionError("reviewer runtime must not activate for this plan")
        yield self._scopes.reviewer_runtime


class FakeExternalQueryRuntime:
    def __init__(self, queries_by_goal: dict[str, str]) -> None:
        self._queries_by_goal = queries_by_goal

    async def call(
        self, agent: object, input: object, *, attempt_number: int
    ) -> ExternalQueryDraft:
        del agent, attempt_number
        return ExternalQueryDraft(
            queries=[self._queries_by_goal[input.task.research_goal]]  # type: ignore[union-attr]
        )


class FakeEvidenceReviewerRuntime:
    """S1: reviewerはRun単位で1回だけ呼ばれるため、goal別ではなく単一draftを返す。"""

    def __init__(
        self, draft: EvidenceReviewerDraft, *, timeline: CallTimeline | None = None
    ) -> None:
        self._draft = draft
        self._timeline = timeline

    async def call(
        self, agent: object, input: object, *, attempt_number: int
    ) -> EvidenceReviewerDraft:
        del agent, input, attempt_number
        if self._timeline is not None:
            self._timeline.record("reviewer.review")
        return self._draft


class FakeFailingReviewerRuntime:
    """常に指定した例外で失敗する reviewer runtime(review失敗経路の検証用)。"""

    def __init__(
        self, error: Exception, *, timeline: CallTimeline | None = None
    ) -> None:
        self._error = error
        self._timeline = timeline

    async def call(
        self, agent: object, input: object, *, attempt_number: int
    ) -> EvidenceReviewerDraft:
        del agent, input, attempt_number
        if self._timeline is not None:
            self._timeline.record("reviewer.review")
        raise self._error


class _ZeroHitExternalTool:
    """全queryに対して常に空のヒットを返す(全taskヒットゼロ経路の検証用)。"""

    async def search(self, request: object) -> list[ExternalSearchHit]:
        del request
        return []


class _UnexpectedReviewerRuntime:
    """精査を呼ばない経路のテストで、誤って呼ばれたら失敗させる。"""

    async def call(
        self, agent: object, input: object, *, attempt_number: int
    ) -> EvidenceReviewerDraft:
        del agent, input, attempt_number
        raise AssertionError(
            "reviewer must not be called when all tasks have zero hits"
        )


class FakeExternalTool:
    def __init__(self, hits_by_query: dict[str, list[ExternalSearchHit]]) -> None:
        self._hits_by_query = hits_by_query

    async def search(self, request: object) -> list[ExternalSearchHit]:
        return list(self._hits_by_query[request.query])  # type: ignore[union-attr]


def _external_runtime_for(
    *,
    plan: object,
    outcome: _RetrievalFixture,
    internal_hits: list[InternalArticleSearchHit] | None = None,
    timeline: CallTimeline | None = None,
    reviewer_runtime: object | None = None,
) -> ExternalScopes:
    """S1: reviewerがRun全体の統合index空間(task_index昇順、group内は内部先・

    外部後)の全選択肢を採用する単一draftを組む(仕様「選択肢の渡し方」)。

    internal_hitsはtask_index 0にのみ帰属させる(このmoduleのfixtureは
    複数taskへの内部ヒット配分を表現しないため、既存の単一task想定を維持する)。
    """
    if outcome.external_search is None:
        raise AssertionError("external outcome must be supplied for an external plan")

    task0_internal_hits = internal_hits or []
    evidence_by_task: dict[int, list[ExternalSearchEvidence]] = {}
    for evidence in outcome.external_search.evidence:
        evidence_by_task.setdefault(evidence.task_index, []).append(evidence)
    reports_by_task = {
        report.task_index: report for report in outcome.external_search.task_reports
    }
    queries_by_goal: dict[str, str] = {}
    hits_by_query: dict[str, list[ExternalSearchHit]] = {}
    selections: list[dict[str, object]] = []
    missing: list[str] = []
    next_index = 0

    for task_index, task in enumerate(plan.research_tasks):
        query = f"fixture-query-{task_index}"
        task_internal_hits = task0_internal_hits if task_index == 0 else []
        task_evidence = evidence_by_task.get(task_index, [])
        hits = [
            ExternalSearchHit(
                url=evidence.url,
                title=evidence.title,
                content=evidence.content,
                published_at=evidence.published_at,
                source_name=evidence.source_name,
            )
            for evidence in task_evidence
        ]
        if not hits:
            hits = [
                ExternalSearchHit(
                    url=f"https://example.com/fixture-{task_index}",
                    title=f"fixture {task_index}",
                )
            ]
        report = reports_by_task.get(task_index)
        queries_by_goal[task.research_goal] = query
        hits_by_query[query] = hits
        group_offset = next_index
        internal_offset = len(task_internal_hits)
        selections.extend(
            {
                "option_index": group_offset + index,
                "claim": "internal claim",
                "why_selected": "internal reviewer explanation",
            }
            for index in range(len(task_internal_hits))
        )
        selections.extend(
            {
                "option_index": group_offset + internal_offset + index,
                "claim": evidence.claim,
                "why_selected": evidence.why_selected,
            }
            for index, evidence in enumerate(task_evidence)
        )
        next_index = group_offset + internal_offset + len(hits)
        if report is not None:
            missing.extend(report.missing)

    draft = EvidenceReviewerDraft.model_validate(
        {"selections": selections, "missing": missing}
    )
    return ExternalScopes(
        external_search=ExternalSearchService(
            query_runtime=FakeExternalQueryRuntime(queries_by_goal),  # type: ignore[arg-type]
            search_gateway=FakeExternalTool(hits_by_query),  # type: ignore[arg-type]
        ),
        reviewer_runtime=(
            reviewer_runtime
            if reviewer_runtime is not None
            else FakeEvidenceReviewerRuntime(draft, timeline=timeline)
        ),
    )


class FakeEvidenceAnswerer:
    def __init__(
        self,
        draft: EvidenceAnswerDraft | Exception,
        *,
        timeline: CallTimeline | None = None,
    ) -> None:
        self._draft = draft
        self._timeline = timeline
        self.calls: list[EvidenceAnswerInput] = []

    async def answer(self, input: EvidenceAnswerInput) -> EvidenceAnswerDraft:
        # S5: review_missingの受け渡し検証はtests/agent/running/
        # test_retrieval_dispatch.pyが正本(条件7)。このfakeは既存の
        # request/evidence/target_time_window契約だけを追跡する。
        if self._timeline is not None:
            self._timeline.record("evidence_answerer.answer")
        self.calls.append(input)
        if isinstance(self._draft, Exception):
            raise self._draft
        return self._draft


class FakeDirectAnswerer:
    def __init__(
        self,
        draft: DirectAnswerDraft | Exception,
        *,
        timeline: CallTimeline | None = None,
    ) -> None:
        self._draft = draft
        self._timeline = timeline
        self.calls: list[DirectAnswerInput] = []

    async def answer(self, input: DirectAnswerInput) -> DirectAnswerDraft:
        if self._timeline is not None:
            self._timeline.record("direct_answerer.answer")
        self.calls.append(input)
        if isinstance(self._draft, Exception):
            raise self._draft
        return self._draft


class CallTimeline:
    def __init__(self) -> None:
        self.events: list[str] = []

    def record(self, event: str) -> None:
        self.events.append(event)


class FakeProgressReporter:
    def __init__(self, *, timeline: CallTimeline | None = None) -> None:
        self._timeline = timeline
        self.stages: list[str] = []

    async def stage_changed(self, stage: str) -> None:
        if self._timeline is not None:
            self._timeline.record(f"progress:{stage}")
        self.stages.append(stage)


class _WorkflowHarness:
    def __init__(
        self,
        *,
        phases: AnsweringPhases,
        progress: FakeProgressReporter | None,
        timeline: CallTimeline | None = None,
    ) -> None:
        self._phases = phases
        self._progress = progress
        self._timeline = timeline

    async def answer(self, input: _WorkflowInput) -> AnswerQuestionResult:
        history = (
            (
                ThreadMessageSnapshot(
                    role="assistant",
                    content=input.previous_answer,
                ),
            )
            if input.previous_answer
            else ()
        )
        runner = AnsweringRunner(
            phases_factory=lambda: self._phases,
            progress=self._progress,
        )
        result = await runner.run(
            RunInput(
                question=input.question,
                history=history,
            ),
            identity=run_identity(
                run_id=UUID("019bd239-1ed4-7fbb-a336-04fe3c197651"),
                as_of=input.as_of,
            ),
        )
        return result.final_output


def _orchestrator(
    *,
    plan: QuestionPlan | Exception,
    outcome: _RetrievalFixture | Exception = AssertionError(
        "retrieval ports must not be called"
    ),
    draft: EvidenceAnswerDraft | Exception = AssertionError(
        "evidence_answerer must not be called"
    ),
    direct_draft: DirectAnswerDraft | Exception = AssertionError(
        "direct answerer must not be called"
    ),
    internal_error: Exception | None = None,
    progress: FakeProgressReporter | None = None,
    timeline: CallTimeline | None = None,
    reviewer_runtime: object | None = None,
    external_runtime_override: ExternalScopes | None = None,
) -> tuple[
    _WorkflowHarness,
    FakePlanner,
    FakeInternalSearch,
    FakeEvidenceAnswerer,
    FakeDirectAnswerer,
]:
    planner = FakePlanner(plan, timeline=timeline)
    internal_search = FakeInternalSearch(
        internal_error if internal_error is not None else outcome,
        timeline=timeline,
    )
    if external_runtime_override is not None:
        external_runtime: ExternalScopes | None = external_runtime_override
    else:
        external_runtime = (
            _external_runtime_for(
                plan=plan,
                outcome=outcome,
                internal_hits=outcome.internal_hits,
                timeline=timeline,
                reviewer_runtime=reviewer_runtime,
            )
            if isinstance(plan, SearchPlan) and isinstance(outcome, _RetrievalFixture)
            else None
        )
    evidence_answerer = FakeEvidenceAnswerer(draft, timeline=timeline)
    direct_answerer = FakeDirectAnswerer(direct_draft, timeline=timeline)
    external_scopes = FakeExternalScopes(external_runtime, timeline=timeline)
    phases = AnsweringPhases(
        planner=planner,
        collector=EvidenceCollectionService(
            internal_search=internal_search,
            external_search_scope_factory=external_scopes.external_search_scope,
        ),
        direct_answerer=direct_answerer,
        evidence_answerer=evidence_answerer,
        reviewer=EvidenceReviewer(
            runtime_scope_factory=external_scopes.reviewer_scope,
        ),
        organizer=PassThroughOrganizer(),
    )
    workflow = _WorkflowHarness(
        phases=phases,
        progress=progress,
        timeline=timeline,
    )
    return workflow, planner, internal_search, evidence_answerer, direct_answerer


@pytest.mark.asyncio
async def test_answer_direct_plan_calls_direct_answerer_only() -> None:
    input_ = _input(
        "前回の結論だけ",
        previous_answer="根拠付き前回答 [[1]]",
    )
    direct_draft = DirectAnswerDraft(answer="こんにちは。何を確認しますか？")
    orchestrator, _, internal_search, evidence_answerer, direct_answerer = (
        _orchestrator(
            plan=_direct_plan(),
            direct_draft=direct_draft,
        )
    )

    result = await orchestrator.answer(input_)

    assert result.status == "answered"
    assert result.answer == direct_draft.answer
    assert result.sources == []
    assert result.missing_aspects == []
    assert result.plan_summary.plan_type == "direct_answer"
    assert direct_answerer.calls == [
        DirectAnswerInput(
            request=AnsweringRequest(
                question=input_.question,
                history=(
                    ThreadMessageSnapshot(
                        role="assistant",
                        content=input_.previous_answer,
                    ),
                ),
                as_of=input_.as_of,
            ),
            previous_answer=input_.previous_answer,
        )
    ]
    assert internal_search.calls == []
    assert evidence_answerer.calls == []


@pytest.mark.asyncio
async def test_answer_direct_plan_orders_progress_and_port_calls() -> None:
    """direct answer経路はplanning→

    answeringの順に報告され、evidence_collectionとevidence_reviewは報告されない。
    """
    timeline = CallTimeline()
    progress = FakeProgressReporter(timeline=timeline)
    orchestrator, _, _, _, _ = _orchestrator(
        plan=_direct_plan(),
        direct_draft=DirectAnswerDraft(answer="直接回答です。"),
        progress=progress,
        timeline=timeline,
    )

    await orchestrator.answer(_input("こんにちは"))

    assert timeline.events == [
        "progress:planning",
        "planner.plan",
        "progress:answering",
        "direct_answerer.answer",
    ]


@pytest.mark.asyncio
async def test_answer_evidence_plan_orders_progress_and_port_calls() -> None:
    """evidence経路はplanning→

    evidence_collection→evidence_review→answeringの順に報告され、各報告は
    対応するport呼び出し(prepare/plan/collect/review/answer)の直前になる。
    """
    timeline = CallTimeline()
    progress = FakeProgressReporter(timeline=timeline)
    orchestrator, _, _, _, _ = _orchestrator(
        plan=_search_plan(),
        outcome=_both_evidence_outcome(),
        draft=_draft(
            answer="根拠から確認できます。",
            cited_refs=["1", "2"],
        ),
        progress=progress,
        timeline=timeline,
    )

    await orchestrator.answer(_input())

    assert timeline.events[:2] == [
        "progress:planning",
        "planner.plan",
    ]
    assert timeline.events[2] == "progress:evidence_collection"
    assert set(timeline.events[3:5]) == {
        "internal_search.search_articles",
        "external_search.activate",
    }
    assert timeline.events[5:] == [
        "progress:evidence_review",
        "reviewer.review",
        "progress:answering",
        "evidence_answerer.answer",
    ]


@pytest.mark.asyncio
async def test_answer_evidence_plan_skips_evidence_review_for_zero_hits() -> None:
    """全taskのヒットが内外ともゼロのRunはreviewerを呼ばずに閉じるため、

    evidence_reviewは報告されず、evidence_collectionの次がansweringになる
    (answering_runner.py:345-353相当の経路)。
    """
    timeline = CallTimeline()
    progress = FakeProgressReporter(timeline=timeline)
    task = _task(0)
    zero_hit_runtime = ExternalScopes(
        external_search=ExternalSearchService(
            query_runtime=FakeExternalQueryRuntime(  # type: ignore[arg-type]
                {task.research_goal: "fixture-query"}
            ),
            search_gateway=_ZeroHitExternalTool(),  # type: ignore[arg-type]
        ),
        reviewer_runtime=_UnexpectedReviewerRuntime(),
    )
    orchestrator, _, _, evidence_answerer, _ = _orchestrator(
        plan=_search_plan(tasks=[task]),
        outcome=_RetrievalFixture(internal_hits=[]),
        external_runtime_override=zero_hit_runtime,
        draft=_draft(
            answer=(
                "検索で引用できる根拠は見つかりませんでした。"
                "一般論としては参考程度に扱ってください。"
            ),
            cited_refs=[],
        ),
        progress=progress,
        timeline=timeline,
    )

    result = await orchestrator.answer(_input())

    assert result.status == "insufficient"
    assert "progress:evidence_review" not in timeline.events
    assert timeline.events[:2] == [
        "progress:planning",
        "planner.plan",
    ]
    assert timeline.events[2] == "progress:evidence_collection"
    assert set(timeline.events[3:5]) == {
        "internal_search.search_articles",
        "external_search.activate",
    }
    assert timeline.events[5:] == [
        "progress:answering",
        "evidence_answerer.answer",
    ]
    assert len(evidence_answerer.calls) == 1
    assert evidence_answerer.calls[0].evidence == ()


@pytest.mark.asyncio
async def test_answer_evidence_plan_reports_evidence_review_when_review_fails() -> None:
    """精査が失敗したRunでもevidence_reviewは報告済みであり、answeringへ進む。

    EvidenceReviewerは2attemptとも失敗した後に根拠ゼロでRunを閉じる
    (retry回数自体はtest_evidence_review.pyが正本、ここではreviewer.review
    呼び出しの前後にどの進捗報告が挟まるかだけを固定する)。
    """
    timeline = CallTimeline()
    progress = FakeProgressReporter(timeline=timeline)
    failure = AgentResponseInvalidError(AgentResponseDefect.OUTPUT_SCHEMA_MISMATCH)
    orchestrator, _, _, _, _ = _orchestrator(
        plan=_search_plan(),
        outcome=_internal_outcome(1),
        reviewer_runtime=FakeFailingReviewerRuntime(failure, timeline=timeline),
        draft=_draft(
            answer=(
                "検索で引用できる根拠は見つかりませんでした。"
                "一般論としては参考程度に扱ってください。"
            ),
            cited_refs=[],
        ),
        progress=progress,
        timeline=timeline,
    )

    result = await orchestrator.answer(_input())

    assert result.status == "insufficient"
    assert timeline.events[:2] == [
        "progress:planning",
        "planner.plan",
    ]
    assert timeline.events[2] == "progress:evidence_collection"
    assert set(timeline.events[3:5]) == {
        "internal_search.search_articles",
        "external_search.activate",
    }
    assert timeline.events[5:] == [
        "progress:evidence_review",
        "reviewer.review",
        "reviewer.review",
        "progress:answering",
        "evidence_answerer.answer",
    ]


@pytest.mark.asyncio
async def test_answer_internal_sources_and_status_from_citations() -> None:
    orchestrator, _, _, _, _ = _orchestrator(
        plan=_search_plan(),
        outcome=_internal_outcome(2),
        draft=_draft(
            answer="内部記事 1 と 2 から確認できます。",
            cited_refs=["1", "2"],
        ),
    )

    result = await orchestrator.answer(_input())

    assert result.status == "answered"
    assert result.plan_summary.plan_type == "search"
    assert [source.source_ref for source in result.sources] == ["1", "2"]
    assert [source.title for source in result.sources] == ["internal 1", "internal 2"]
    assert result.missing_aspects == []


@pytest.mark.asyncio
async def test_answer_external_source_is_cited_source_only() -> None:
    orchestrator, _, _, _, _ = _orchestrator(
        plan=_search_plan(),
        outcome=_external_outcome_only(),
        draft=_draft(
            answer="外部根拠から確認できます。",
            cited_refs=["1"],
        ),
    )

    result = await orchestrator.answer(_input())

    assert result.status == "answered"
    assert result.plan_summary.plan_type == "search"
    assert len(result.sources) == 1
    assert isinstance(result.sources[0], ExternalUrlSource)


@pytest.mark.asyncio
async def test_answer_search_plan_with_both_evidence_types_cited() -> None:
    orchestrator, _, _, _, _ = _orchestrator(
        plan=_search_plan(),
        outcome=_both_evidence_outcome(),
        draft=_draft(
            answer="内部根拠と外部根拠から確認できます。",
            cited_refs=["1", "2"],
        ),
    )

    result = await orchestrator.answer(_input())

    assert result.status == "answered"
    assert result.plan_summary.plan_type == "search"
    assert [source.source_ref for source in result.sources] == ["1", "2"]


@pytest.mark.asyncio
async def test_answer_search_plan_omits_unused_external_source() -> None:
    orchestrator, _, _, _, _ = _orchestrator(
        plan=_search_plan(),
        outcome=_both_evidence_outcome(),
        draft=_draft(
            answer="内部根拠だけで確認できます。",
            cited_refs=["1"],
        ),
    )

    result = await orchestrator.answer(_input())

    assert result.status == "answered"
    assert result.plan_summary.plan_type == "search"
    assert [source.source_ref for source in result.sources] == ["1"]
    assert all(not isinstance(source, ExternalUrlSource) for source in result.sources)


@pytest.mark.asyncio
async def test_answer_empty_retrieval_evidence_calls_synthesis() -> None:
    """evidence空のRunでもsynthesisは呼ばれ、機構由来のmissing_aspects

    (文言の正本はtest_result_assembly.py)によりstatusはinsufficientになる。
    """
    draft = _draft(
        answer=(
            "検索で引用できる根拠は見つかりませんでした。"
            "一般論としては参考程度に扱ってください。"
        ),
        cited_refs=[],
    )
    orchestrator, _, _, evidence_answerer, _ = _orchestrator(
        plan=_search_plan(),
        outcome=_internal_outcome(0),
        draft=draft,
    )

    result = await orchestrator.answer(_input())

    assert result.status == "insufficient"
    assert result.answer == draft.answer
    assert result.sources == []
    assert result.missing_aspects
    assert len(evidence_answerer.calls) == 1
    assert evidence_answerer.calls[0].evidence == ()


@pytest.mark.asyncio
async def test_answer_missing_aspects_are_ordered_and_deduplicated() -> None:
    """review.missingのtask_index順連結と重複排除を検証する

    (draftはmissing_aspectsを持たなくなったため、task別missingだけが対象)。
    """
    tasks = [_task(0), _task(1)]
    reports = [
        _report(task_index=1, missing=["市場予想値", "会社側コメント"]),
        _report(task_index=0, missing=["市場予想値", "実績値"]),
    ]
    orchestrator, _, _, _, _ = _orchestrator(
        plan=_search_plan(tasks=tasks),
        outcome=_RetrievalFixture(
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
        ),
        draft=_draft(answer="根拠が不足しています。", cited_refs=["1"]),
    )

    result = await orchestrator.answer(_input())

    assert result.missing_aspects == [
        "市場予想値",
        "実績値",
        "会社側コメント",
    ]


@pytest.mark.asyncio
async def test_answer_rejects_unknown_citation_ref() -> None:
    orchestrator, _, _, _, _ = _orchestrator(
        plan=_search_plan(),
        outcome=_internal_outcome(1),
        draft=_draft(
            answer="存在しない根拠を引用しています。",
            cited_refs=["2"],
        ),
    )

    with pytest.raises(EvidenceAnswerDraftInvalidError, match="unknown citation ref"):
        await orchestrator.answer(_input())


@pytest.mark.asyncio
async def test_answer_deduplicates_repeated_citation_refs_in_source_order() -> None:
    orchestrator, _, _, _, _ = _orchestrator(
        plan=_search_plan(),
        outcome=_internal_outcome(2),
        draft=_draft(
            answer="重複引用を含みます。",
            cited_refs=["2", "1", "2", "1"],
        ),
    )

    result = await orchestrator.answer(_input())

    assert [source.source_ref for source in result.sources] == ["1", "2"]


@pytest.mark.asyncio
async def test_answer_passes_pipeline_inputs_and_variant_time_window() -> None:
    input_ = _input()
    orchestrator, planner, internal_search, evidence_answerer, _ = _orchestrator(
        plan=_search_plan(
            # _both_evidence_outcome()はtask_index=0のみを表現するため1 taskに揃える。
            tasks=[_task(0)],
            target_time_window=TargetTimeWindow(kind="last_n_days", days=1),
        ),
        outcome=_both_evidence_outcome(),
        draft=_draft(
            answer="確認できます。",
            cited_refs=["1", "2"],
        ),
    )

    await orchestrator.answer(input_)

    assert planner.calls == [
        PlanningInput(question=input_.question, as_of=input_.as_of)
    ]
    assert internal_search.calls == [InternalSearchQueries(queries=("NVIDIA AI GPU",))]
    assert evidence_answerer.calls[0].request == AnsweringRequest(
        question=input_.question,
        as_of=input_.as_of,
    )
    assert evidence_answerer.calls[0].target_time_window == TargetTimeWindow(
        kind="last_n_days", days=1
    )
    assert evidence_answerer.calls[0].review_missing == ()


@pytest.mark.asyncio
async def test_answer_passes_none_time_window_for_search_plan() -> None:
    orchestrator, _, _, evidence_answerer, _ = _orchestrator(
        plan=_search_plan(),
        outcome=_internal_outcome(1),
        draft=_draft(
            answer="確認できます。",
            cited_refs=["1"],
        ),
    )

    await orchestrator.answer(_input())

    assert evidence_answerer.calls[0].target_time_window is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    (
        "plan",
        "outcome",
        "internal_error",
        "draft",
        "message",
        "expected_planner_timeline",
    ),
    [
        (
            RuntimeError("planner failed"),
            _RetrievalFixture(),
            None,
            AssertionError("evidence_answerer must not be called"),
            "planner failed",
            [
                "progress:planning",
                "planner.plan",
            ],
        ),
        (
            lambda: _search_plan(),
            _internal_outcome(0),
            RuntimeError("internal search failed"),
            AssertionError("evidence_answerer must not be called"),
            "internal search failed",
            None,
        ),
        (
            lambda: _search_plan(),
            _internal_outcome(1),
            None,
            RuntimeError("evidence_answerer failed"),
            "evidence_answerer failed",
            None,
        ),
    ],
)
async def test_answer_step_failure_stops_before_later_progress_or_ports(
    plan: QuestionPlan | Exception | object,
    outcome: _RetrievalFixture | Exception,
    internal_error: Exception | None,
    draft: EvidenceAnswerDraft | Exception,
    message: str,
    expected_planner_timeline: list[str] | None,
) -> None:
    timeline = CallTimeline()
    resolved_plan = plan() if callable(plan) else plan
    orchestrator, _, _, _, _ = _orchestrator(
        plan=resolved_plan,
        outcome=outcome,
        draft=draft,
        internal_error=internal_error,
        progress=FakeProgressReporter(timeline=timeline),
        timeline=timeline,
    )

    with pytest.raises(RuntimeError, match=message):
        await orchestrator.answer(_input())

    if expected_planner_timeline is not None:
        assert timeline.events == expected_planner_timeline
        return

    evidence_collection_index = timeline.events.index("progress:evidence_collection")
    branch_start_indices = {
        event: timeline.events.index(event)
        for event in (
            "internal_search.search_articles",
            "external_search.activate",
        )
    }
    assert all(
        index > evidence_collection_index for index in branch_start_indices.values()
    )
    if internal_error is not None:
        assert "progress:evidence_review" not in timeline.events
        assert "progress:answering" not in timeline.events
        assert "evidence_answerer.answer" not in timeline.events
        return

    answering_index = timeline.events.index("progress:answering")
    assert all(index < answering_index for index in branch_start_indices.values())
    assert timeline.events.index("evidence_answerer.answer") > answering_index


@pytest.mark.asyncio
async def test_answer_direct_failure_stops_before_later_progress_or_ports() -> None:
    timeline = CallTimeline()
    orchestrator, _, _, _, _ = _orchestrator(
        plan=_direct_plan(),
        direct_draft=RuntimeError("direct failed"),
        progress=FakeProgressReporter(timeline=timeline),
        timeline=timeline,
    )

    with pytest.raises(RuntimeError, match="direct failed"):
        await orchestrator.answer(_input("こんにちは"))

    assert timeline.events == [
        "progress:planning",
        "planner.plan",
        "progress:answering",
        "direct_answerer.answer",
    ]
