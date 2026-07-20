"""AnsweringRunnerが所有するworkflow順序の契約テスト。"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import pytest

from app.agent.answering.contract import AnsweringRequest
from app.agent.answering.direct_answer.contract import DirectAnswerDraft
from app.agent.answering.evidence_answer.contract import EvidenceAnswerDraft
from app.agent.evidence_collection.external_search import ExternalSearchOutcome
from app.agent.evidence_collection.internal_search.query_embedding import (
    InternalSearchQueries,
)
from app.agent.planning.contract import (
    InternalRetrievalPlan,
    NoRetrievalPlan,
    PlanningRequest,
    QuestionPlan,
)
from app.agent.question_context import (
    QuestionContext,
    QuestionContextPreparationResult,
    QuestionContextTelemetry,
)
from app.agent.running import AnsweringPhases, AnsweringRunner, RunContext, RunInput

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

    async def prepare(self, **_kwargs: object) -> QuestionContextPreparationResult:
        self._timeline.append("prepare")
        if self._error is not None:
            raise self._error
        return QuestionContextPreparationResult(
            context=self._context,
            telemetry=QuestionContextTelemetry(),
        )


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

    async def search_articles(
        self,
        queries: InternalSearchQueries,
    ) -> list[object]:
        self._timeline.append("internal_search")
        self.calls.append(queries)
        return []


class _UnreachableExternalSearch:
    async def search(
        self,
        _tasks: list[object],
        *,
        target_time_window: str | None,
        as_of: datetime,
        external: object,
    ) -> ExternalSearchOutcome:
        raise AssertionError(
            "external search must not run: "
            f"{target_time_window!r} {as_of!r} {external!r}"
        )


class _UnreachableExternalRuntimeFactory:
    def activate(self) -> object:
        raise AssertionError("external runtime must not activate")


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
        target_time_window: str | None,
    ) -> EvidenceAnswerDraft:
        self._timeline.append("evidence_answerer")
        self.calls.append(
            {
                "request": request,
                "evidence": evidence,
                "target_time_window": target_time_window,
            }
        )
        return EvidenceAnswerDraft(
            sufficiency="insufficient",
            answer="根拠が不足しています",
            missing_aspects=["根拠不足"],
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
            internal_search=internal_search,
            external_runtime_factory=_UnreachableExternalRuntimeFactory(),
            direct_answerer=direct_answerer,
            evidence_answerer=evidence_answerer,
        )

    return (
        AnsweringRunner(
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
        plan=NoRetrievalPlan(reason="検索不要"),
        timeline=timeline,
        context=context,
    )

    result = await runner.run(
        RunInput(question="元の質問", history=()),
        run_context=RunContext(run_id=RUN_ID, as_of=AS_OF),
        hooks=_Hooks(timeline),
    )

    assert timeline == [
        "prepare",
        "hook",
        "phases_factory",
        "progress:planning",
        "planner",
        "progress:synthesizing",
        "direct_answerer",
    ]
    assert planner.calls[0].context is context
    assert direct_answerer.calls[0][0].context is context
    assert result.context.question_context is context
    assert internal_search.calls == []
    assert evidence_answerer.calls == []


async def test_retrieval_workflow_order_and_non_selected_port() -> None:
    timeline: list[str] = []
    context = QuestionContext(standalone_question="整理済みの質問")
    plan = InternalRetrievalPlan(
        internal_queries=["検索語"],
        reason="内部根拠が必要",
    )
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

    assert timeline == [
        "prepare",
        "hook",
        "phases_factory",
        "progress:planning",
        "planner",
        "progress:retrieving",
        "internal_search",
        "progress:synthesizing",
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
        plan=NoRetrievalPlan(reason="検索不要"),
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
    expected_timeline = (
        ["prepare"] if failure_point == "prepare" else ["prepare", "hook"]
    )
    assert timeline == expected_timeline
