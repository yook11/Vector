"""AnsweringRunner が所有する retrieval dispatch の契約テスト。"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime
from uuid import UUID

import pytest

from app.agent.answering.contract import AnsweringRequest
from app.agent.answering.direct_answer.contract import DirectAnswerDraft
from app.agent.answering.evidence_answer.contract import EvidenceAnswerDraft
from app.agent.contract import AnswerProgressStage
from app.agent.evidence_collection.external_search import (
    ExternalResearchRuntime,
    ExternalSearchEvidence,
    ExternalSearchOutcome,
    ExternalSearchResearchRunner,
    ExternalSearchService,
    ResearchTaskReport,
)
from app.agent.evidence_collection.internal_search.article_search import (
    InternalArticleContent,
    InternalArticleSearchHit,
)
from app.agent.evidence_collection.internal_search.contract import InternalSearchError
from app.agent.evidence_collection.internal_search.query_embedding import (
    InternalSearchQueries,
)
from app.agent.planning.contract import (
    ExternalResearchTask,
    ExternalSearchPlan,
    InternalAndExternalPlan,
    InternalRetrievalPlan,
    PlanningRequest,
    QuestionPlan,
)
from app.agent.question_context import (
    QuestionContext,
    QuestionContextPreparationResult,
    QuestionContextTelemetry,
)
from app.agent.running import AnsweringPhases, AnsweringRunner, RunContext, RunInput
from app.analysis.analyzed_article import InScopeAnalyzedArticle
from app.analysis.assessment.domain.result import InScope, InScopeCategory
from tests.agent.runtime._fakes import ScriptedAgentRuntime

RUN_ID = UUID("019bd239-1ed4-7fbb-a336-04fe3c197652")
AS_OF = datetime(2026, 7, 20, 9, 30, tzinfo=UTC)


def _internal_plan(*queries: str) -> InternalRetrievalPlan:
    return InternalRetrievalPlan(
        internal_queries=list(queries or ("NVIDIA AI GPU",)),
        reason="internal evidence is required",
    )


def _external_plan() -> ExternalSearchPlan:
    return ExternalSearchPlan(
        external_research_tasks=[_task("NVIDIA の供給を確認する")],
        target_time_window="直近24時間",
        reason="external evidence is required",
    )


def _mixed_plan() -> InternalAndExternalPlan:
    return InternalAndExternalPlan(
        internal_queries=["NVIDIA AI GPU", "Blackwell supply"],
        external_research_tasks=[_task("NVIDIA の供給を確認する")],
        target_time_window="直近24時間",
        reason="both evidence sources are required",
    )


def _task(goal: str) -> ExternalResearchTask:
    return ExternalResearchTask(collection_goal=goal)


def _hit(
    *, assessment_id: int = 1001, title: str = "NVIDIA"
) -> InternalArticleSearchHit:
    article = InScopeAnalyzedArticle(
        curation_id=assessment_id - 1000,
        title=title,
        summary=f"{title} summary",
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


def _external_outcome(
    tasks: list[ExternalResearchTask],
    *,
    status: str = "succeeded",
    evidence: Sequence[ExternalSearchEvidence] = (),
) -> ExternalSearchOutcome:
    return ExternalSearchOutcome(
        tasks=tasks,
        evidence=list(evidence),
        task_reports=[
            ResearchTaskReport(
                task_index=index,
                collection_goal=task.collection_goal,
                status=status,  # type: ignore[arg-type]
                evidence_count=sum(item.task_index == index for item in evidence),
                missing=["provider unavailable"] if status != "succeeded" else [],
            )
            for index, task in enumerate(tasks)
        ],
        effective_agent_count=len(tasks),
    )


def _external_evidence() -> ExternalSearchEvidence:
    return ExternalSearchEvidence(
        source_ref="external-0-0",
        task_index=0,
        claim="NVIDIA announced a supply update.",
        why_selected="primary evidence candidate",
        url="https://example.com/nvidia-supply",
        title="external supply update",
    )


class _Preparer:
    async def prepare(self, **_kwargs: object) -> QuestionContextPreparationResult:
        return QuestionContextPreparationResult(
            context=QuestionContext(standalone_question="NVIDIA の見通しは？"),
            telemetry=QuestionContextTelemetry(),
        )


class _Planner:
    def __init__(
        self,
        plan: QuestionPlan,
        *,
        error: BaseException | None = None,
    ) -> None:
        self._plan = plan
        self._error = error
        self.calls: list[PlanningRequest] = []

    async def plan(self, request: PlanningRequest) -> QuestionPlan:
        self.calls.append(request)
        if self._error is not None:
            raise self._error
        return self._plan


class _InternalSearch:
    def __init__(
        self,
        *,
        hits: Sequence[InternalArticleSearchHit] = (),
        error: BaseException | None = None,
        started: asyncio.Event | None = None,
        release: asyncio.Event | None = None,
        raised: asyncio.Event | None = None,
        timeline: list[str] | None = None,
    ) -> None:
        self._hits = list(hits)
        self._error = error
        self._started = started
        self._release = release
        self._raised = raised
        self._timeline = timeline
        self.calls: list[InternalSearchQueries] = []
        self.completed = False
        self.cancelled_error: asyncio.CancelledError | None = None
        self.finished = asyncio.Event()

    async def search_articles(
        self,
        queries: InternalSearchQueries,
    ) -> list[InternalArticleSearchHit]:
        self.calls.append(queries)
        try:
            if self._started is not None:
                self._started.set()
            if self._release is not None:
                await self._release.wait()
            if self._error is not None:
                if self._raised is not None:
                    self._raised.set()
                raise self._error
            self.completed = True
            return list(self._hits)
        except asyncio.CancelledError as exc:
            self.cancelled_error = exc
            raise
        finally:
            if self._timeline is not None:
                self._timeline.append("internal.finished")
            self.finished.set()


class _ExternalSearch:
    def __init__(
        self,
        outcome: ExternalSearchOutcome,
        *,
        error: BaseException | None = None,
        started: asyncio.Event | None = None,
        release: asyncio.Event | None = None,
        raised: asyncio.Event | None = None,
        timeline: list[str] | None = None,
    ) -> None:
        self._outcome = outcome
        self._error = error
        self._started = started
        self._release = release
        self._raised = raised
        self._timeline = timeline
        self.calls: list[
            tuple[list[ExternalResearchTask], str | None, datetime, object]
        ] = []
        self.completed = False
        self.cancelled_error: asyncio.CancelledError | None = None
        self.finished = asyncio.Event()

    async def search(
        self,
        external_research_tasks: list[ExternalResearchTask],
        *,
        target_time_window: str | None,
        as_of: datetime,
        external: object,
    ) -> ExternalSearchOutcome:
        if self._timeline is not None:
            self._timeline.append("external.search")
        self.calls.append(
            (external_research_tasks, target_time_window, as_of, external)
        )
        try:
            if self._started is not None:
                self._started.set()
            if self._release is not None:
                await self._release.wait()
            if self._error is not None:
                if self._raised is not None:
                    self._raised.set()
                raise self._error
            self.completed = True
            return self._outcome
        except asyncio.CancelledError as exc:
            self.cancelled_error = exc
            raise
        finally:
            if self._timeline is not None:
                self._timeline.append("external.finished")
            self.finished.set()


class _Scope(AbstractAsyncContextManager[object]):
    def __init__(
        self,
        external: object,
        *,
        timeline: list[str] | None = None,
        exit_reached: asyncio.Event | None = None,
        exit_error: BaseException | None = None,
    ) -> None:
        self.external = external
        self._timeline = timeline
        self._exit_reached = exit_reached
        self._exit_error = exit_error
        self.entered = False
        self.exited = False
        self.exit_calls = 0
        self.close_succeeded = False
        self.body_exception: BaseException | None = None

    async def __aenter__(self) -> object:
        self.entered = True
        if self._timeline is not None:
            self._timeline.append("scope.enter")
        return self.external

    async def __aexit__(
        self,
        exc_type: object,
        exc: BaseException | None,
        traceback: object,
    ) -> bool:
        self.exit_calls += 1
        self.exited = True
        self.body_exception = exc
        if self._timeline is not None:
            self._timeline.append("scope.exit")
        if self._exit_reached is not None:
            self._exit_reached.set()
        if self._exit_error is not None:
            raise self._exit_error
        self.close_succeeded = True
        return False


class _ExternalRuntimeFactory:
    def __init__(
        self,
        externals: Sequence[object] = (),
        *,
        timeline: list[str] | None = None,
        exit_reached: asyncio.Event | None = None,
        activation_error: BaseException | None = None,
        activation_reached: asyncio.Event | None = None,
        exit_error: BaseException | None = None,
    ) -> None:
        self._externals = list(externals)
        self._timeline = timeline
        self._exit_reached = exit_reached
        self._activation_error = activation_error
        self._activation_reached = activation_reached
        self._exit_error = exit_error
        self.scopes: list[_Scope] = []
        self.activate_calls = 0

    def activate(self) -> _Scope:
        self.activate_calls += 1
        if self._activation_reached is not None:
            self._activation_reached.set()
        if self._activation_error is not None:
            raise self._activation_error
        external = self._externals.pop(0) if self._externals else object()
        scope = _Scope(
            external,
            timeline=self._timeline,
            exit_reached=self._exit_reached,
            exit_error=self._exit_error,
        )
        self.scopes.append(scope)
        return scope


class _EvidenceAnswerer:
    def __init__(
        self,
        *,
        timeline: list[str] | None = None,
        error: BaseException | None = None,
    ) -> None:
        self._timeline = timeline
        self._error = error
        self.calls: list[list[object]] = []

    async def answer(
        self,
        *,
        request: AnsweringRequest,
        evidence: list[object],
        target_time_window: str | None,
    ) -> EvidenceAnswerDraft:
        if self._timeline is not None:
            self._timeline.append("evidence_answerer.start")
        self.calls.append(list(evidence))
        if self._error is not None:
            raise self._error
        return EvidenceAnswerDraft(
            sufficiency="insufficient",
            answer="取得できた根拠では十分に回答できません。",
            missing_aspects=["根拠が不足しています"],
        )


class _Progress:
    def __init__(
        self,
        timeline: list[str],
        *,
        error_stage: AnswerProgressStage | None = None,
        error: BaseException | None = None,
    ) -> None:
        self._timeline = timeline
        self._error_stage = error_stage
        self._error = error

    async def stage_changed(self, stage: AnswerProgressStage) -> None:
        self._timeline.append(f"progress.{stage}")
        if stage == self._error_stage and self._error is not None:
            raise self._error


class _TaskFailureAfterSiblingStartsRuntime:
    def __init__(self, *, error: BaseException, timeline: list[str]) -> None:
        self._error = error
        self._timeline = timeline
        self.blocking_task_started = asyncio.Event()
        self.blocking_task_finished = asyncio.Event()
        self.blocking_task_cancelled = False

    async def invoke(
        self,
        agent: object,
        input: object,
        *,
        attempt_number: int,
    ) -> object:
        del agent, attempt_number
        task = input.task  # type: ignore[union-attr]
        if task.collection_goal == "failing task":
            await self.blocking_task_started.wait()
            raise self._error
        self.blocking_task_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.blocking_task_cancelled = True
            raise
        finally:
            self._timeline.append("sibling.finished")
            self.blocking_task_finished.set()
        raise AssertionError("blocking task must be cancelled")


class _BlockingQueryRuntime:
    def __init__(self, *, timeline: list[str]) -> None:
        self._timeline = timeline
        self.started = asyncio.Event()
        self.finished = asyncio.Event()
        self.cancelled_error: asyncio.CancelledError | None = None

    async def invoke(
        self,
        agent: object,
        input: object,
        *,
        attempt_number: int,
    ) -> object:
        del agent, input, attempt_number
        self.started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError as exc:
            self.cancelled_error = exc
            raise
        finally:
            self._timeline.append("query_child.finished")
            self.finished.set()
        raise AssertionError("query child must be cancelled")


class _UnreachableExternalSearchTool:
    @property
    def name(self) -> str:
        return "external_search"

    async def invoke(self, input: object) -> list[object]:
        raise AssertionError(f"search tool must not run: {input!r}")


class _UnreachableDirectAnswerer:
    async def answer(
        self,
        *,
        request: AnsweringRequest,
        previous_answer: str = "",
    ) -> DirectAnswerDraft:
        raise AssertionError("direct answerer must not run for a retrieval plan")


def _runner(
    *,
    plan: QuestionPlan,
    internal: _InternalSearch,
    external: _ExternalSearch | ExternalSearchService,
    factory: _ExternalRuntimeFactory,
    evidence_answerer: _EvidenceAnswerer | None = None,
    progress: _Progress | None = None,
    planner_error: BaseException | None = None,
) -> AnsweringRunner:
    phases = AnsweringPhases(
        planner=_Planner(plan, error=planner_error),
        internal_search=internal,
        external_search=external,
        external_runtime_factory=factory,
        direct_answerer=_UnreachableDirectAnswerer(),
        evidence_answerer=evidence_answerer or _EvidenceAnswerer(),
    )
    return AnsweringRunner(
        context_preparer=_Preparer(),
        phases_factory=lambda: phases,
        progress=progress,
    )


async def _run(runner: AnsweringRunner):
    return await runner.run(
        RunInput(question="NVIDIA の見通しは？", history=()),
        run_context=RunContext(run_id=RUN_ID, as_of=AS_OF),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("plan_factory", "expected_internal_calls", "expected_external_calls"),
    [
        pytest.param(_internal_plan, 1, 0, id="internal"),
        pytest.param(_external_plan, 0, 1, id="external"),
        pytest.param(_mixed_plan, 1, 1, id="mixed"),
    ],
)
async def test_runner_selects_only_retrieval_ports_for_plan_variant(
    plan_factory: object,
    expected_internal_calls: int,
    expected_external_calls: int,
) -> None:
    plan = plan_factory()  # type: ignore[operator]
    internal = _InternalSearch()
    external = _ExternalSearch(
        _external_outcome(getattr(plan, "external_research_tasks", []))
    )
    factory = _ExternalRuntimeFactory()

    await _run(
        _runner(plan=plan, internal=internal, external=external, factory=factory)
    )

    assert (
        len(internal.calls),
        len(external.calls),
        len(factory.scopes),
    ) == (
        expected_internal_calls,
        expected_external_calls,
        expected_external_calls,
    )


@pytest.mark.asyncio
async def test_runner_preserves_internal_query_order() -> None:
    plan = _internal_plan("NVIDIA", "nvidia", "OpenAI")
    internal = _InternalSearch()
    external = _ExternalSearch(_external_outcome([]))

    await _run(
        _runner(
            plan=plan,
            internal=internal,
            external=external,
            factory=_ExternalRuntimeFactory(),
        )
    )

    assert internal.calls == [
        InternalSearchQueries(queries=("NVIDIA", "nvidia", "OpenAI"))
    ]


@pytest.mark.asyncio
async def test_runner_preserves_internal_hit_order_into_synthesis() -> None:
    answerer = _EvidenceAnswerer()
    first = _hit(assessment_id=1001, title="first internal hit")
    second = _hit(assessment_id=1002, title="second internal hit")

    await _run(
        _runner(
            plan=_internal_plan(),
            internal=_InternalSearch(hits=[first, second]),
            external=_ExternalSearch(_external_outcome([])),
            factory=_ExternalRuntimeFactory(),
            evidence_answerer=answerer,
        )
    )

    assert [item.source.title for item in answerer.calls[0]] == [
        "first internal hit",
        "second internal hit",
    ]


@pytest.mark.asyncio
async def test_runner_passes_external_plan_values_and_borrowed_runtime_identity() -> (
    None
):
    plan = _external_plan()
    runtime = object()
    internal = _InternalSearch()
    external = _ExternalSearch(_external_outcome(plan.external_research_tasks))
    factory = _ExternalRuntimeFactory([runtime])

    await _run(
        _runner(plan=plan, internal=internal, external=external, factory=factory)
    )

    assert (
        external.calls,
        factory.scopes[0].entered,
        factory.scopes[0].exited,
    ) == (
        [
            (
                plan.external_research_tasks,
                plan.target_time_window,
                AS_OF,
                runtime,
            )
        ],
        True,
        True,
    )


@pytest.mark.asyncio
async def test_mixed_run_releases_external_scope_while_internal_branch_is_pending() -> (
    None
):
    plan = _mixed_plan()
    internal_started = asyncio.Event()
    internal_release = asyncio.Event()
    scope_exited = asyncio.Event()
    internal = _InternalSearch(started=internal_started, release=internal_release)
    factory = _ExternalRuntimeFactory(exit_reached=scope_exited)
    running = asyncio.create_task(
        _run(
            _runner(
                plan=plan,
                internal=internal,
                external=_ExternalSearch(
                    _external_outcome(plan.external_research_tasks)
                ),
                factory=factory,
            )
        )
    )

    try:
        await asyncio.wait_for(scope_exited.wait(), timeout=0.5)
        assert (
            internal_started.is_set(),
            running.done(),
            factory.scopes[0].exited,
        ) == (True, False, True)
    finally:
        internal_release.set()
        await asyncio.wait_for(running, timeout=0.5)


@pytest.mark.asyncio
async def test_external_scope_exits_before_synthesis_progress_and_answerer_start() -> (
    None
):
    plan = _external_plan()
    timeline: list[str] = []

    await _run(
        _runner(
            plan=plan,
            internal=_InternalSearch(),
            external=_ExternalSearch(
                _external_outcome(plan.external_research_tasks),
                timeline=timeline,
            ),
            factory=_ExternalRuntimeFactory(timeline=timeline),
            evidence_answerer=_EvidenceAnswerer(timeline=timeline),
            progress=_Progress(timeline),
        )
    )

    assert (
        timeline.index("scope.exit")
        < timeline.index("progress.synthesizing")
        < timeline.index("evidence_answerer.start")
    )


@pytest.mark.asyncio
async def test_external_scope_exits_after_runner_joins_failed_task_sibling() -> None:
    error = RuntimeError("unclassified task failure")
    timeline: list[str] = []
    query_runtime = _TaskFailureAfterSiblingStartsRuntime(
        error=error,
        timeline=timeline,
    )
    external_runtime = ExternalResearchRuntime(
        query_runtime=query_runtime,  # type: ignore[arg-type]
        selector_runtime=ScriptedAgentRuntime([]),
        search_tool=_UnreachableExternalSearchTool(),  # type: ignore[arg-type]
    )
    factory = _ExternalRuntimeFactory(
        [external_runtime],
        timeline=timeline,
    )
    plan = ExternalSearchPlan(
        external_research_tasks=[_task("failing task"), _task("blocking task")],
        target_time_window="直近24時間",
        reason="verify resource cleanup ordering",
    )

    with pytest.raises(RuntimeError) as raised:
        await asyncio.wait_for(
            _run(
                _runner(
                    plan=plan,
                    internal=_InternalSearch(),
                    external=ExternalSearchService(
                        runner=ExternalSearchResearchRunner()
                    ),
                    factory=factory,
                )
            ),
            timeout=0.5,
        )

    assert (
        raised.value is error,
        query_runtime.blocking_task_cancelled,
        query_runtime.blocking_task_finished.is_set(),
        factory.scopes[0].exited,
        timeline.index("sibling.finished") < timeline.index("scope.exit"),
    ) == (True, True, True, True, True)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_stage", ["planning", "retrieving"])
async def test_pre_dispatch_failure_does_not_activate_external_scope(
    failure_stage: str,
) -> None:
    error = RuntimeError(f"{failure_stage} failure")
    factory = _ExternalRuntimeFactory()
    progress = (
        _Progress([], error_stage="retrieving", error=error)
        if failure_stage == "retrieving"
        else None
    )

    with pytest.raises(RuntimeError) as raised:
        await _run(
            _runner(
                plan=_external_plan(),
                internal=_InternalSearch(),
                external=_ExternalSearch(
                    _external_outcome(_external_plan().external_research_tasks)
                ),
                factory=factory,
                progress=progress,
                planner_error=error if failure_stage == "planning" else None,
            )
        )

    assert (raised.value is error, factory.activate_calls, factory.scopes) == (
        True,
        0,
        [],
    )


@pytest.mark.asyncio
async def test_same_runner_uses_fresh_external_scope_and_runtime_per_run() -> None:
    plan = _external_plan()
    first_runtime = object()
    second_runtime = object()
    external = _ExternalSearch(_external_outcome(plan.external_research_tasks))
    factory = _ExternalRuntimeFactory([first_runtime, second_runtime])
    runner = _runner(
        plan=plan,
        internal=_InternalSearch(),
        external=external,
        factory=factory,
    )

    await _run(runner)
    await _run(runner)

    first_scope, second_scope = factory.scopes
    assert (
        factory.activate_calls,
        first_scope is not second_scope,
        external.calls[0][3] is first_runtime,
        external.calls[1][3] is second_runtime,
        first_scope.exit_calls,
        second_scope.exit_calls,
        first_scope.close_succeeded,
        second_scope.close_succeeded,
    ) == (2, True, True, True, 1, 1, True, True)


@pytest.mark.asyncio
async def test_classified_external_outcome_closes_scope_once() -> None:
    plan = _external_plan()
    factory = _ExternalRuntimeFactory()

    await _run(
        _runner(
            plan=plan,
            internal=_InternalSearch(),
            external=_ExternalSearch(
                _external_outcome(
                    plan.external_research_tasks,
                    status="provider_failed",
                )
            ),
            factory=factory,
        )
    )

    assert (
        factory.activate_calls,
        factory.scopes[0].exit_calls,
        factory.scopes[0].close_succeeded,
    ) == (1, 1, True)


@pytest.mark.asyncio
async def test_external_unknown_error_closes_scope_before_identity_propagation() -> (
    None
):
    plan = _external_plan()
    error = RuntimeError("external unknown failure")
    factory = _ExternalRuntimeFactory()

    with pytest.raises(RuntimeError) as raised:
        await _run(
            _runner(
                plan=plan,
                internal=_InternalSearch(),
                external=_ExternalSearch(
                    _external_outcome(plan.external_research_tasks),
                    error=error,
                ),
                factory=factory,
            )
        )

    scope = factory.scopes[0]
    assert (
        raised.value is error,
        scope.exit_calls,
        scope.close_succeeded,
        scope.body_exception is error,
    ) == (True, 1, True, True)


@pytest.mark.asyncio
async def test_answerer_failure_starts_after_external_scope_closes() -> None:
    plan = _external_plan()
    error = RuntimeError("evidence answer failure")
    timeline: list[str] = []
    factory = _ExternalRuntimeFactory(timeline=timeline)

    with pytest.raises(RuntimeError) as raised:
        await _run(
            _runner(
                plan=plan,
                internal=_InternalSearch(),
                external=_ExternalSearch(
                    _external_outcome(plan.external_research_tasks),
                    timeline=timeline,
                ),
                factory=factory,
                evidence_answerer=_EvidenceAnswerer(
                    timeline=timeline,
                    error=error,
                ),
            )
        )

    scope = factory.scopes[0]
    assert (
        raised.value is error,
        scope.exit_calls,
        scope.close_succeeded,
        timeline.index("scope.exit") < timeline.index("evidence_answerer.start"),
    ) == (True, 1, True, True)


@pytest.mark.asyncio
async def test_runner_starts_mixed_retrieval_branches_concurrently() -> None:
    plan = _mixed_plan()
    internal_started = asyncio.Event()
    external_started = asyncio.Event()
    internal = _InternalSearch(started=internal_started, release=external_started)
    external = _ExternalSearch(
        _external_outcome(plan.external_research_tasks),
        started=external_started,
        release=internal_started,
    )

    await asyncio.wait_for(
        _run(
            _runner(
                plan=plan,
                internal=internal,
                external=external,
                factory=_ExternalRuntimeFactory(),
            )
        ),
        timeout=0.5,
    )

    assert (internal_started.is_set(), external_started.is_set()) == (True, True)


@pytest.mark.asyncio
async def test_runner_converts_only_internal_search_error_to_failure_value() -> None:
    internal = _InternalSearch(error=InternalSearchError(phase="article_search"))
    result = await _run(
        _runner(
            plan=_internal_plan(),
            internal=internal,
            external=_ExternalSearch(_external_outcome([])),
            factory=_ExternalRuntimeFactory(),
        )
    )

    assert result.final_output.retrieval.collection_failures == ["internal_search"]


@pytest.mark.asyncio
async def test_runner_keeps_external_outcome_on_classified_internal_failure() -> None:
    plan = _mixed_plan()
    external_evidence = _external_evidence()
    external_outcome = _external_outcome(
        plan.external_research_tasks,
        evidence=[external_evidence],
    )
    answerer = _EvidenceAnswerer()
    result = await _run(
        _runner(
            plan=plan,
            internal=_InternalSearch(error=InternalSearchError(phase="article_search")),
            external=_ExternalSearch(external_outcome),
            factory=_ExternalRuntimeFactory(),
            evidence_answerer=answerer,
        )
    )

    assert (
        result.final_output.retrieval.collection_failures,
        [item.source.title for item in answerer.calls[0]],
    ) == (["internal_search"], [external_evidence.title])


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["external", "mixed"])
async def test_runner_keeps_failed_external_task_reports_as_outcome(
    mode: str,
) -> None:
    plan: QuestionPlan = _external_plan() if mode == "external" else _mixed_plan()
    tasks = getattr(plan, "external_research_tasks")
    answerer = _EvidenceAnswerer()
    result = await _run(
        _runner(
            plan=plan,
            internal=_InternalSearch(hits=[_hit()] if mode == "mixed" else []),
            external=_ExternalSearch(
                _external_outcome(tasks, status="provider_failed")
            ),
            factory=_ExternalRuntimeFactory(),
            evidence_answerer=answerer,
        )
    )

    assert (
        result.final_output.retrieval.collection_failures,
        "provider unavailable" in result.final_output.missing_aspects,
        [item.source.title for item in answerer.calls[0]],
    ) == (
        [],
        True,
        ["NVIDIA"] if mode == "mixed" else [],
    )


@pytest.mark.asyncio
async def test_runner_treats_zero_internal_hits_as_success() -> None:
    result = await _run(
        _runner(
            plan=_internal_plan(),
            internal=_InternalSearch(hits=[]),
            external=_ExternalSearch(_external_outcome([])),
            factory=_ExternalRuntimeFactory(),
        )
    )

    assert result.final_output.retrieval.collection_failures == []


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["internal", "external"])
async def test_runner_propagates_unknown_single_branch_error_by_identity(
    mode: str,
) -> None:
    error = RuntimeError(f"{mode} unknown failure")
    plan: QuestionPlan = _internal_plan() if mode == "internal" else _external_plan()
    internal = _InternalSearch(error=error if mode == "internal" else None)
    external = _ExternalSearch(
        _external_outcome(getattr(plan, "external_research_tasks", [])),
        error=error if mode == "external" else None,
    )

    with pytest.raises(RuntimeError) as raised:
        await _run(
            _runner(
                plan=plan,
                internal=internal,
                external=external,
                factory=_ExternalRuntimeFactory(),
            )
        )

    assert raised.value is error


@pytest.mark.asyncio
async def test_runner_waits_for_sibling_before_external_error_propagates() -> None:
    plan = _mixed_plan()
    error = RuntimeError("external unknown failure")
    internal_started = asyncio.Event()
    internal_release = asyncio.Event()
    external_raised = asyncio.Event()
    internal = _InternalSearch(started=internal_started, release=internal_release)
    external = _ExternalSearch(
        _external_outcome(plan.external_research_tasks),
        error=error,
        release=internal_started,
        raised=external_raised,
    )
    task = asyncio.create_task(
        _run(
            _runner(
                plan=plan,
                internal=internal,
                external=external,
                factory=_ExternalRuntimeFactory(),
            )
        )
    )

    await asyncio.wait_for(external_raised.wait(), timeout=0.5)
    assert not task.done()
    internal_release.set()
    with pytest.raises(RuntimeError) as raised:
        await asyncio.wait_for(task, timeout=0.5)

    assert (raised.value is error, internal.completed) == (True, True)


@pytest.mark.asyncio
async def test_runner_waits_for_external_before_internal_error_propagates() -> None:
    plan = _mixed_plan()
    error = RuntimeError("internal unknown failure")
    external_started = asyncio.Event()
    external_release = asyncio.Event()
    internal_raised = asyncio.Event()
    internal = _InternalSearch(
        error=error,
        release=external_started,
        raised=internal_raised,
    )
    external = _ExternalSearch(
        _external_outcome(plan.external_research_tasks),
        started=external_started,
        release=external_release,
    )
    task = asyncio.create_task(
        _run(
            _runner(
                plan=plan,
                internal=internal,
                external=external,
                factory=_ExternalRuntimeFactory(),
            )
        )
    )

    await asyncio.wait_for(internal_raised.wait(), timeout=0.5)
    assert not task.done()
    external_release.set()
    with pytest.raises(RuntimeError) as raised:
        await asyncio.wait_for(task, timeout=0.5)

    assert (raised.value is error, external.completed) == (True, True)


@pytest.mark.asyncio
async def test_runner_prefers_internal_error_after_mixed_branches_finish() -> None:
    plan = _mixed_plan()
    internal_error = RuntimeError("internal unknown failure")
    external_error = RuntimeError("external unknown failure")
    internal_started = asyncio.Event()
    external_started = asyncio.Event()
    internal = _InternalSearch(
        error=internal_error,
        started=internal_started,
        release=external_started,
    )
    external = _ExternalSearch(
        _external_outcome(plan.external_research_tasks),
        error=external_error,
        started=external_started,
        release=internal_started,
    )

    with pytest.raises(RuntimeError) as raised:
        await asyncio.wait_for(
            _run(
                _runner(
                    plan=plan,
                    internal=internal,
                    external=external,
                    factory=_ExternalRuntimeFactory(),
                )
            ),
            timeout=0.5,
        )

    assert raised.value is internal_error


@pytest.mark.asyncio
async def test_mixed_activation_failure_waits_for_internal_before_propagation() -> None:
    plan = _mixed_plan()
    activation_error = RuntimeError("external activation failure")
    activation_reached = asyncio.Event()
    internal_started = asyncio.Event()
    internal_release = asyncio.Event()
    internal = _InternalSearch(started=internal_started, release=internal_release)
    factory = _ExternalRuntimeFactory(
        activation_error=activation_error,
        activation_reached=activation_reached,
    )
    running = asyncio.create_task(
        _run(
            _runner(
                plan=plan,
                internal=internal,
                external=_ExternalSearch(
                    _external_outcome(plan.external_research_tasks)
                ),
                factory=factory,
            )
        )
    )

    await asyncio.wait_for(internal_started.wait(), timeout=0.5)
    await asyncio.wait_for(activation_reached.wait(), timeout=0.5)
    assert not running.done()
    internal_release.set()
    with pytest.raises(RuntimeError) as raised:
        await asyncio.wait_for(running, timeout=0.5)

    assert (
        raised.value is activation_error,
        internal.completed,
        factory.activate_calls,
        factory.scopes,
    ) == (True, True, 1, [])


@pytest.mark.asyncio
async def test_external_only_close_failure_propagates_same_sentinel() -> None:
    plan = _external_plan()
    close_error = RuntimeError("external close failure")
    factory = _ExternalRuntimeFactory(exit_error=close_error)

    with pytest.raises(RuntimeError) as raised:
        await _run(
            _runner(
                plan=plan,
                internal=_InternalSearch(),
                external=_ExternalSearch(
                    _external_outcome(plan.external_research_tasks)
                ),
                factory=factory,
            )
        )

    scope = factory.scopes[0]
    assert (
        raised.value is close_error,
        scope.exit_calls,
        scope.close_succeeded,
        scope.body_exception,
    ) == (True, 1, False, None)


@pytest.mark.asyncio
async def test_external_only_close_failure_replaces_unknown_body_error() -> None:
    plan = _external_plan()
    body_error = RuntimeError("external body failure")
    close_error = RuntimeError("external close failure")
    factory = _ExternalRuntimeFactory(exit_error=close_error)

    with pytest.raises(RuntimeError) as raised:
        await _run(
            _runner(
                plan=plan,
                internal=_InternalSearch(),
                external=_ExternalSearch(
                    _external_outcome(plan.external_research_tasks),
                    error=body_error,
                ),
                factory=factory,
            )
        )

    scope = factory.scopes[0]
    assert (
        raised.value is close_error,
        close_error.__context__ is body_error,
        scope.body_exception is body_error,
        scope.exit_calls,
        scope.close_succeeded,
    ) == (True, True, True, 1, False)


@pytest.mark.asyncio
@pytest.mark.parametrize("internal_fails", [False, True])
async def test_mixed_close_failure_waits_then_applies_internal_priority(
    internal_fails: bool,
) -> None:
    plan = _mixed_plan()
    close_error = RuntimeError("external close failure")
    internal_error = RuntimeError("internal unknown failure")
    internal_started = asyncio.Event()
    internal_release = asyncio.Event()
    scope_exit_reached = asyncio.Event()
    internal = _InternalSearch(
        error=internal_error if internal_fails else None,
        started=internal_started,
        release=internal_release,
    )
    factory = _ExternalRuntimeFactory(
        exit_reached=scope_exit_reached,
        exit_error=close_error,
    )
    running = asyncio.create_task(
        _run(
            _runner(
                plan=plan,
                internal=internal,
                external=_ExternalSearch(
                    _external_outcome(plan.external_research_tasks)
                ),
                factory=factory,
            )
        )
    )

    await asyncio.wait_for(internal_started.wait(), timeout=0.5)
    await asyncio.wait_for(scope_exit_reached.wait(), timeout=0.5)
    assert not running.done()
    internal_release.set()
    with pytest.raises(RuntimeError) as raised:
        await asyncio.wait_for(running, timeout=0.5)

    expected_error = internal_error if internal_fails else close_error
    scope = factory.scopes[0]
    assert (
        raised.value is expected_error,
        internal.finished.is_set(),
        scope.exit_calls,
        scope.close_succeeded,
        scope.body_exception,
    ) == (True, True, 1, False, None)


@pytest.mark.asyncio
async def test_mixed_outer_cancellation_joins_branches_before_scope_close() -> None:
    plan = _mixed_plan()
    timeline: list[str] = []
    internal_started = asyncio.Event()
    external_started = asyncio.Event()
    internal = _InternalSearch(
        started=internal_started,
        release=asyncio.Event(),
        timeline=timeline,
    )
    external = _ExternalSearch(
        _external_outcome(plan.external_research_tasks),
        started=external_started,
        release=asyncio.Event(),
        timeline=timeline,
    )
    scope_exit_reached = asyncio.Event()
    factory = _ExternalRuntimeFactory(
        timeline=timeline,
        exit_reached=scope_exit_reached,
    )
    running = asyncio.create_task(
        _run(
            _runner(
                plan=plan,
                internal=internal,
                external=external,
                factory=factory,
            )
        )
    )

    await asyncio.wait_for(internal_started.wait(), timeout=0.5)
    await asyncio.wait_for(external_started.wait(), timeout=0.5)
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(running, timeout=0.5)

    scope = factory.scopes[0]
    assert (
        internal.cancelled_error is not None,
        external.cancelled_error is not None,
        internal.finished.is_set(),
        external.finished.is_set(),
        scope_exit_reached.is_set(),
        scope.exit_calls,
        scope.close_succeeded,
        scope.body_exception is external.cancelled_error,
        timeline.index("external.finished") < timeline.index("scope.exit"),
    ) == (
        True,
        True,
        True,
        True,
        True,
        1,
        True,
        True,
        True,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("first_failure", ["internal", "external"])
async def test_mixed_outer_cancellation_wins_over_completed_unknown_failure(
    first_failure: str,
) -> None:
    plan = _mixed_plan()
    first_error = RuntimeError(f"{first_failure} unknown failure")
    first_raised = asyncio.Event()
    internal_started = asyncio.Event()
    external_started = asyncio.Event()
    timeline: list[str] = []
    blocked_release = asyncio.Event()
    internal = _InternalSearch(
        error=first_error if first_failure == "internal" else None,
        started=internal_started,
        release=external_started if first_failure == "internal" else blocked_release,
        raised=first_raised if first_failure == "internal" else None,
        timeline=timeline,
    )
    external = _ExternalSearch(
        _external_outcome(plan.external_research_tasks),
        error=first_error if first_failure == "external" else None,
        started=external_started,
        release=internal_started if first_failure == "external" else blocked_release,
        raised=first_raised if first_failure == "external" else None,
        timeline=timeline,
    )
    scope_exit_reached = asyncio.Event()
    factory = _ExternalRuntimeFactory(
        timeline=timeline,
        exit_reached=scope_exit_reached,
    )
    running = asyncio.create_task(
        _run(
            _runner(
                plan=plan,
                internal=internal,
                external=external,
                factory=factory,
            )
        )
    )

    await asyncio.wait_for(first_raised.wait(), timeout=0.5)
    assert not running.done()
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(running, timeout=0.5)

    cancelled_branch = external if first_failure == "internal" else internal
    completed_branch = internal if first_failure == "internal" else external
    assert (
        cancelled_branch.cancelled_error is not None,
        completed_branch.cancelled_error,
        internal.finished.is_set(),
        external.finished.is_set(),
        scope_exit_reached.is_set(),
        factory.scopes[0].exit_calls,
        factory.scopes[0].close_succeeded,
    ) == (
        True,
        None,
        True,
        True,
        True,
        1,
        True,
    )


@pytest.mark.asyncio
async def test_external_only_close_failure_replaces_body_cancellation() -> None:
    plan = _external_plan()
    close_error = RuntimeError("close failed during cancellation")
    external_started = asyncio.Event()
    timeline: list[str] = []
    external = _ExternalSearch(
        _external_outcome(plan.external_research_tasks),
        started=external_started,
        release=asyncio.Event(),
        timeline=timeline,
    )
    scope_exit_reached = asyncio.Event()
    factory = _ExternalRuntimeFactory(
        timeline=timeline,
        exit_reached=scope_exit_reached,
        exit_error=close_error,
    )
    running = asyncio.create_task(
        _run(
            _runner(
                plan=plan,
                internal=_InternalSearch(),
                external=external,
                factory=factory,
            )
        )
    )

    await asyncio.wait_for(external_started.wait(), timeout=0.5)
    running.cancel()
    with pytest.raises(RuntimeError) as raised:
        await asyncio.wait_for(running, timeout=0.5)

    scope = factory.scopes[0]
    assert (
        raised.value is close_error,
        external.cancelled_error is not None,
        raised.value.__context__ is external.cancelled_error,
        scope.body_exception is external.cancelled_error,
        external.finished.is_set(),
        scope_exit_reached.is_set(),
        scope.exit_calls,
        timeline.index("external.finished") < timeline.index("scope.exit"),
    ) == (True, True, True, True, True, True, 1, True)


@pytest.mark.asyncio
async def test_mixed_outer_cancellation_drains_close_and_query_child_failure() -> None:
    plan = _mixed_plan()
    close_error = RuntimeError("close failed during mixed cancellation")
    timeline: list[str] = []
    internal_started = asyncio.Event()
    internal = _InternalSearch(
        started=internal_started,
        release=asyncio.Event(),
        timeline=timeline,
    )
    query_runtime = _BlockingQueryRuntime(timeline=timeline)
    external_runtime = ExternalResearchRuntime(
        query_runtime=query_runtime,  # type: ignore[arg-type]
        selector_runtime=ScriptedAgentRuntime([]),
        search_tool=_UnreachableExternalSearchTool(),  # type: ignore[arg-type]
    )
    scope_exit_reached = asyncio.Event()
    factory = _ExternalRuntimeFactory(
        [external_runtime],
        timeline=timeline,
        exit_reached=scope_exit_reached,
        exit_error=close_error,
    )
    running = asyncio.create_task(
        _run(
            _runner(
                plan=plan,
                internal=internal,
                external=ExternalSearchService(runner=ExternalSearchResearchRunner()),
                factory=factory,
            )
        )
    )

    await asyncio.wait_for(internal_started.wait(), timeout=0.5)
    await asyncio.wait_for(query_runtime.started.wait(), timeout=0.5)
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(running, timeout=0.5)

    scope = factory.scopes[0]
    assert (
        internal.cancelled_error is not None,
        query_runtime.cancelled_error is not None,
        internal.finished.is_set(),
        query_runtime.finished.is_set(),
        scope_exit_reached.is_set(),
        scope.exit_calls,
        scope.close_succeeded,
        close_error.__context__ is scope.body_exception,
        timeline.index("query_child.finished") < timeline.index("scope.exit"),
    ) == (
        True,
        True,
        True,
        True,
        True,
        1,
        False,
        True,
        True,
    )
