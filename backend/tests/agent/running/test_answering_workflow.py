"""AnsweringRunnerが所有するworkflow順序の契約テスト。"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from uuid import UUID

from app.agent.answering.contract import AnsweringRequest
from app.agent.answering.direct_answer.contract import (
    DirectAnswerDraft,
    DirectAnswerInput,
)
from app.agent.answering.evidence_answer.contract import (
    EvidenceAnswerDraft,
    EvidenceAnswerInput,
)
from app.agent.evidence_collection import EvidenceCollectionService
from app.agent.evidence_collection.external_search import ExternalSearchService
from app.agent.evidence_collection.external_search.contract import ExternalQueryDraft
from app.agent.evidence_collection.internal_search.query_embedding import (
    InternalSearchQueries,
)
from app.agent.evidence_review import EvidenceReviewer
from app.agent.planning.contract import (
    DirectAnswerPlan,
    PlanningInput,
    QuestionPlan,
    ResearchTask,
    SearchPlan,
)
from app.agent.running import AnsweringPhases, AnsweringRunner, RunInput
from tests.agent.running._harness import (
    PassThroughOrganizer,
    fixed_scope,
    run_identity,
)

RUN_ID = UUID("019bd239-1ed4-7fbb-a336-04fe3c197650")
AS_OF = datetime(2026, 7, 19, 9, 30, tzinfo=UTC)


class _Planner:
    def __init__(self, plan: QuestionPlan, timeline: list[str]) -> None:
        self._plan = plan
        self._timeline = timeline
        self.calls: list[PlanningInput] = []

    async def plan(self, input: PlanningInput) -> QuestionPlan:
        self._timeline.append("planner")
        self.calls.append(input)
        return self._plan


class _InternalSearch:
    def __init__(self, timeline: list[str]) -> None:
        self._timeline = timeline
        self.calls: list[InternalSearchQueries] = []

    async def search(self, queries: InternalSearchQueries) -> list[object]:
        self._timeline.append("internal_search")
        self.calls.append(queries)
        return []


def _direct_plan() -> DirectAnswerPlan:
    return DirectAnswerPlan()


def _search_plan() -> SearchPlan:
    return SearchPlan(
        research_tasks=[
            ResearchTask(
                research_goal="外部根拠を確認する",
                article_search_queries=["検索語"],
            )
        ],
    )


class _EmptyExternalQueryRuntime:
    async def call(
        self, agent: object, input: object, *, attempt_number: int
    ) -> ExternalQueryDraft:
        del agent, input, attempt_number
        return ExternalQueryDraft(queries=[])


class _EmptyExternalSearchScope:
    def __init__(self, timeline: list[str]) -> None:
        self._timeline = timeline

    @asynccontextmanager
    async def __call__(self):
        self._timeline.append("external_search_scope")
        yield ExternalSearchService(
            query_runtime=_EmptyExternalQueryRuntime(),  # type: ignore[arg-type]
            search_gateway=object(),  # type: ignore[arg-type]
        )


class _DirectAnswerer:
    def __init__(self, timeline: list[str]) -> None:
        self._timeline = timeline
        self.calls: list[tuple[AnsweringRequest, str]] = []

    async def answer(self, input: DirectAnswerInput) -> DirectAnswerDraft:
        self._timeline.append("direct_answerer")
        self.calls.append((input.request, input.previous_answer))
        return DirectAnswerDraft(answer="直接回答")


class _EvidenceAnswerer:
    def __init__(self, timeline: list[str]) -> None:
        self._timeline = timeline
        self.calls: list[EvidenceAnswerInput] = []

    async def answer(self, input: EvidenceAnswerInput) -> EvidenceAnswerDraft:
        # S5: review_missingの受け渡し検証はtests/agent/running/
        # test_retrieval_dispatch.pyが正本(条件7)。このfakeはworkflowの
        # 呼び出し順序を見るためのものであり、既存契約だけを追跡する。
        self._timeline.append("evidence_answerer")
        self.calls.append(input)
        return EvidenceAnswerDraft(
            answer="確認できた範囲で回答します。"
            + "".join(f"[[{item.source.source_ref}]]" for item in input.evidence),
            cited_refs=[item.source.source_ref for item in input.evidence],
        )


class _Progress:
    def __init__(self, timeline: list[str]) -> None:
        self._timeline = timeline

    async def stage_changed(self, stage: str) -> None:
        self._timeline.append(f"progress:{stage}")


def _runner(
    *,
    plan: QuestionPlan,
    timeline: list[str],
) -> tuple[
    AnsweringRunner,
    _Planner,
    _InternalSearch,
    _DirectAnswerer,
    _EvidenceAnswerer,
]:
    planner = _Planner(plan, timeline)
    internal_search = _InternalSearch(timeline)
    direct_answerer = _DirectAnswerer(timeline)
    evidence_answerer = _EvidenceAnswerer(timeline)

    def phases_factory() -> AnsweringPhases:
        timeline.append("phases_factory")
        return AnsweringPhases(
            planner=planner,
            collector=EvidenceCollectionService(
                internal_search=internal_search,
                external_search_scope_factory=_EmptyExternalSearchScope(timeline),
            ),
            direct_answerer=direct_answerer,
            evidence_answerer=evidence_answerer,
            reviewer=EvidenceReviewer(runtime_scope_factory=fixed_scope(object())),
            organizer=PassThroughOrganizer(),
        )

    return (
        AnsweringRunner(
            phases_factory=phases_factory,
            progress=_Progress(timeline),
        ),
        planner,
        internal_search,
        direct_answerer,
        evidence_answerer,
    )


async def test_direct_workflow_order_and_question_passthrough() -> None:
    timeline: list[str] = []
    runner, planner, internal_search, direct_answerer, evidence_answerer = _runner(
        plan=_direct_plan(),
        timeline=timeline,
    )

    await runner.run(
        RunInput(question="元の質問", history=()),
        identity=run_identity(run_id=RUN_ID, as_of=AS_OF),
    )

    assert timeline == [
        "phases_factory",
        "progress:planning",
        "planner",
        "progress:answering",
        "direct_answerer",
    ]
    assert planner.calls[0].question == "元の質問"
    assert direct_answerer.calls[0][0].question == "元の質問"
    assert internal_search.calls == []
    assert evidence_answerer.calls == []


async def test_search_workflow_starts_both_retrieval_ports() -> None:
    timeline: list[str] = []
    plan = _search_plan()
    runner, planner, internal_search, direct_answerer, evidence_answerer = _runner(
        plan=plan,
        timeline=timeline,
    )

    result = await runner.run(
        RunInput(question="元の質問", history=()),
        identity=run_identity(run_id=RUN_ID, as_of=AS_OF),
    )

    assert timeline[:4] == [
        "phases_factory",
        "progress:planning",
        "planner",
        "progress:evidence_collection",
    ]
    assert set(timeline[4:6]) == {"internal_search", "external_search_scope"}
    # ヒットが内外ともゼロのため精査は呼ばれず、evidence_review は報告されない。
    assert timeline[6:] == [
        "progress:answering",
        "evidence_answerer",
    ]
    assert planner.calls[0].question == "元の質問"
    assert internal_search.calls == [InternalSearchQueries(queries=("検索語",))]
    assert evidence_answerer.calls[0].request.question == "元の質問"
    assert direct_answerer.calls == []
    assert result.final_output.status == "insufficient"
