"""AnsweringRunnerが所有するworkflow順序の契約テスト。"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from uuid import UUID

import pytest

import app.agent.planning.contract as planning_contract
from app.agent.answering.contract import AnsweringRequest
from app.agent.answering.direct_answer.contract import DirectAnswerDraft
from app.agent.answering.evidence_answer.contract import (
    EvidenceAnswerOutcome,
    EvidenceAnswerUnavailable,
)
from app.agent.evidence_collection import NewsCollector, Researcher
from app.agent.evidence_collection.evidence_review import EvidenceReviewer
from app.agent.evidence_collection.external_search import ExternalResearchRuntime
from app.agent.evidence_collection.external_search.contract import ExternalQueryDraft
from app.agent.evidence_collection.internal_search.query_embedding import (
    InternalSearchQueries,
)
from app.agent.planning.contract import (
    PlanningRequest,
    QuestionPlan,
    TargetTimeWindow,
)
from app.agent.question_context import QuestionContext
from app.agent.running import AnsweringPhases, AnsweringRunner, RunContext, RunInput
from tests.agent.running._input_safety import AllowInputSafetyChecker

RUN_ID = UUID("019bd239-1ed4-7fbb-a336-04fe3c197650")
AS_OF = datetime(2026, 7, 19, 9, 30, tzinfo=UTC)


class _Preparer:
    def __init__(
        self,
        context: QuestionContext,
        timeline: list[str],
        error: BaseException | None = None,
    ) -> None:
        self._context = context
        self._timeline = timeline
        self._error = error

    async def prepare(self, **_kwargs: object) -> QuestionContext:
        self._timeline.append("prepare")
        if self._error is not None:
            raise self._error
        return self._context


class _Hooks:
    def __init__(
        self,
        timeline: list[str],
        error: BaseException | None = None,
    ) -> None:
        self._timeline = timeline
        self._error = error

    async def on_answering_context_prepared(self, **_kwargs: object) -> None:
        self._timeline.append("hook")
        if self._error is not None:
            raise self._error


class _Planner:
    def __init__(self, plan: QuestionPlan, timeline: list[str]) -> None:
        self._plan = plan
        self._timeline = timeline
        self.calls: list[PlanningRequest] = []

    async def plan(self, request: PlanningRequest) -> QuestionPlan:
        self._timeline.append("planner")
        self.calls.append(request)
        return self._plan


class _InternalSearch:
    def __init__(self, timeline: list[str]) -> None:
        self._timeline = timeline
        self.calls: list[InternalSearchQueries] = []

    @property
    def name(self) -> str:
        return "internal_search"

    async def invoke(self, input: object) -> list[object]:
        self._timeline.append("internal_search")
        self.calls.append(input.queries)  # type: ignore[attr-defined]
        return []


def _plan_type(name: str) -> object:
    value = getattr(planning_contract, name, None)
    if value is None:
        pytest.fail(f"planning contract must define {name}")
    return value


def _direct_plan() -> object:
    return _plan_type("DirectAnswerPlan")()


def _search_plan() -> object:
    return _plan_type("SearchPlan")(
        research_tasks=[
            _plan_type("ResearchTask")(
                research_goal="外部根拠を確認する",
                article_search_queries=["検索語"],
            )
        ],
    )


class _EmptyExternalQueryRuntime:
    async def invoke(
        self, agent: object, input: object, *, attempt_number: int
    ) -> ExternalQueryDraft:
        del agent, input, attempt_number
        return ExternalQueryDraft(queries=[])


class _EmptyExternalRuntimeFactory:
    def __init__(self, timeline: list[str]) -> None:
        self._timeline = timeline

    @asynccontextmanager
    async def activate(self):
        self._timeline.append("external_runtime")
        yield ExternalResearchRuntime(
            query_runtime=_EmptyExternalQueryRuntime(),  # type: ignore[arg-type]
            reviewer_runtime=object(),  # type: ignore[arg-type]
            search_tool=object(),  # type: ignore[arg-type]
        )


class _DirectAnswerer:
    def __init__(self, timeline: list[str]) -> None:
        self._timeline = timeline
        self.calls: list[tuple[AnsweringRequest, str]] = []

    async def answer(
        self,
        *,
        request: AnsweringRequest,
        previous_answer: str = "",
    ) -> DirectAnswerDraft:
        self._timeline.append("direct_answerer")
        self.calls.append((request, previous_answer))
        return DirectAnswerDraft(answer="直接回答")


class _EvidenceAnswerer:
    def __init__(self, timeline: list[str]) -> None:
        self._timeline = timeline
        self.calls: list[dict[str, object]] = []

    async def answer(
        self,
        *,
        request: AnsweringRequest,
        evidence: list[object],
        target_time_window: TargetTimeWindow | None,
        review_missing: tuple[str, ...] = (),
    ) -> EvidenceAnswerOutcome:
        # S5: review_missingの受け渡し検証はtests/agent/running/
        # test_retrieval_dispatch.pyが正本(条件7)。このfakeはworkflowの
        # 呼び出し順序を見るためのものであり、既存契約だけを追跡する。
        del review_missing
        self._timeline.append("evidence_answerer")
        self.calls.append(
            {
                "request": request,
                "evidence": evidence,
                "target_time_window": target_time_window,
            }
        )
        # evidenceの内容を問わず「回答を作れなかった」を演じる。
        return EvidenceAnswerUnavailable(failure_code="fake_evidence_unavailable")


class _Progress:
    def __init__(self, timeline: list[str]) -> None:
        self._timeline = timeline

    async def stage_changed(self, stage: str) -> None:
        self._timeline.append(f"progress:{stage}")


def _runner(
    *,
    plan: QuestionPlan,
    timeline: list[str],
    context: QuestionContext,
    prepare_error: BaseException | None = None,
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
            collector=NewsCollector(
                researcher=Researcher(internal_search=internal_search)
            ),
            external_runtime_factory=_EmptyExternalRuntimeFactory(timeline),
            direct_answerer=direct_answerer,
            evidence_answerer=evidence_answerer,
            reviewer=EvidenceReviewer(),
        )

    return (
        AnsweringRunner(
            input_safety_checker=AllowInputSafetyChecker(),
            context_preparer=_Preparer(context, timeline, prepare_error),
            phases_factory=phases_factory,
            progress=_Progress(timeline),
        ),
        planner,
        internal_search,
        direct_answerer,
        evidence_answerer,
    )


async def test_direct_workflow_order_and_context_identity() -> None:
    timeline: list[str] = []
    context = QuestionContext(standalone_question="整理済みの質問")
    runner, planner, internal_search, direct_answerer, evidence_answerer = _runner(
        plan=_direct_plan(),
        timeline=timeline,
        context=context,
    )

    result = await runner.run(
        RunInput(question="元の質問", history=()),
        run_context=RunContext(run_id=RUN_ID, as_of=AS_OF),
        hooks=_Hooks(timeline),
    )

    assert timeline == [
        "progress:safety_check",
        "progress:context_resolution",
        "prepare",
        "hook",
        "phases_factory",
        "progress:planning",
        "planner",
        "progress:answering",
        "direct_answerer",
    ]
    assert planner.calls[0].context is context
    assert direct_answerer.calls[0][0].context is context
    assert result.context.question_context is context
    assert internal_search.calls == []
    assert evidence_answerer.calls == []


async def test_search_workflow_starts_both_retrieval_ports() -> None:
    timeline: list[str] = []
    context = QuestionContext(standalone_question="整理済みの質問")
    plan = _search_plan()
    runner, planner, internal_search, direct_answerer, evidence_answerer = _runner(
        plan=plan,
        timeline=timeline,
        context=context,
    )

    result = await runner.run(
        RunInput(question="元の質問", history=()),
        run_context=RunContext(run_id=RUN_ID, as_of=AS_OF),
        hooks=_Hooks(timeline),
    )

    assert timeline[:8] == [
        "progress:safety_check",
        "progress:context_resolution",
        "prepare",
        "hook",
        "phases_factory",
        "progress:planning",
        "planner",
        "progress:evidence_collection",
    ]
    assert set(timeline[8:10]) == {"internal_search", "external_runtime"}
    # 候補が内外ともゼロのため精査は呼ばれず、evidence_review は報告されない。
    assert timeline[10:] == [
        "progress:answering",
        "evidence_answerer",
    ]
    assert planner.calls[0].context is context
    assert internal_search.calls == [InternalSearchQueries(queries=("検索語",))]
    assert evidence_answerer.calls[0]["request"].context is context
    assert direct_answerer.calls == []
    assert result.final_output.status == "insufficient"


@pytest.mark.parametrize("failure_point", ["prepare", "hook"])
async def test_preparation_or_hook_failure_does_not_build_phases(
    failure_point: str,
) -> None:
    timeline: list[str] = []
    error = RuntimeError(f"{failure_point} failed")
    context = QuestionContext(standalone_question="整理済みの質問")
    runner, *_ = _runner(
        plan=_direct_plan(),
        timeline=timeline,
        context=context,
        prepare_error=error if failure_point == "prepare" else None,
    )

    with pytest.raises(RuntimeError) as raised:
        await runner.run(
            RunInput(question="元の質問", history=()),
            run_context=RunContext(run_id=RUN_ID, as_of=AS_OF),
            hooks=_Hooks(
                timeline,
                error=error if failure_point == "hook" else None,
            ),
        )

    assert raised.value is error
    reported_stages = ["progress:safety_check", "progress:context_resolution"]
    expected_timeline = (
        [*reported_stages, "prepare"]
        if failure_point == "prepare"
        else [*reported_stages, "prepare", "hook"]
    )
    assert timeline == expected_timeline
