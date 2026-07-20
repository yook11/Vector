"""AnsweringRunner の retrieval dispatch と external resource scope 契約。"""

from __future__ import annotations

import asyncio
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pytest
from logfire.testing import CaptureLogfire

from app.agent.answering.contract import AnsweringRequest
from app.agent.answering.direct_answer.contract import DirectAnswerDraft
from app.agent.answering.evidence_answer.contract import EvidenceAnswerDraft
from app.agent.contract import AnswerProgressStage
from app.agent.evidence_collection.external_search import (
    ExternalResearchRuntime,
    ExternalSearchCandidate,
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
    TargetTimeWindow,
)
from app.agent.question_context import (
    QuestionContext,
    QuestionContextPreparationResult,
    QuestionContextTelemetry,
)
from app.agent.running import AnsweringPhases, AnsweringRunner, RunContext, RunInput
from app.agent.running import answering_runner as answering_runner_module
from app.analysis.analyzed_article import InScopeAnalyzedArticle
from app.analysis.assessment.domain.result import InScope, InScopeCategory
from tests.agent.runtime._fakes import ScriptedAgentRuntime
from tests.logfire._metric_helpers import collected_metrics

RUN_CONTEXT = RunContext(
    run_id=UUID("019bd239-1ed4-7fbb-a336-04fe3c197652"),
    as_of=datetime(2026, 7, 20, 9, 30, tzinfo=UTC),
)
_TARGET_TIME_WINDOW = TargetTimeWindow(kind="last_n_days", days=1)


def _task(goal: str = "NVIDIA の供給を確認する") -> ExternalResearchTask:
    return ExternalResearchTask(collection_goal=goal)


def _query_draft() -> Any:
    from app.agent.evidence_collection.external_search.contract import (
        ExternalQueryDraft,
    )

    return ExternalQueryDraft(queries=["NVIDIA supply"])


def _selection_draft() -> Any:
    from app.agent.evidence_collection.external_search.contract import (
        ExternalEvidenceSelectionDraft,
    )

    return ExternalEvidenceSelectionDraft(selections=[], missing=[])


def _candidate(url: str, *, title: str) -> ExternalSearchCandidate:
    return ExternalSearchCandidate(url=url, title=title, snippet="snippet")


def _hit(*, assessment_id: int, title: str) -> InternalArticleSearchHit:
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


class _Preparer:
    async def prepare(self, **_kwargs: object) -> QuestionContextPreparationResult:
        return QuestionContextPreparationResult(
            context=QuestionContext(standalone_question="NVIDIA の見通しは？"),
            telemetry=QuestionContextTelemetry(),
        )


class _Planner:
    def __init__(
        self, plan: QuestionPlan, *, error: BaseException | None = None
    ) -> None:
        self._plan = plan
        self._error = error

    async def plan(self, request: PlanningRequest) -> QuestionPlan:
        del request
        if self._error is not None:
            raise self._error
        return self._plan


class _InternalSearch:
    def __init__(
        self,
        *,
        hits: list[InternalArticleSearchHit] | None = None,
        error: BaseException | None = None,
        release: asyncio.Event | None = None,
        raised: asyncio.Event | None = None,
        timeline: list[str] | None = None,
    ) -> None:
        self._hits = hits or []
        self._error = error
        self._release = release
        self._raised = raised
        self._timeline = timeline
        self.started = asyncio.Event()
        self.finished = asyncio.Event()
        self.calls: list[InternalSearchQueries] = []
        self.completed = False
        self.cancelled_error: asyncio.CancelledError | None = None

    async def search_articles(
        self, queries: InternalSearchQueries
    ) -> list[InternalArticleSearchHit]:
        self.calls.append(queries)
        self.started.set()
        try:
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


class _Tool:
    def __init__(
        self,
        results: dict[str, list[ExternalSearchCandidate]] | None = None,
        *,
        errors: dict[str, BaseException] | None = None,
        started: asyncio.Event | None = None,
        release: asyncio.Event | None = None,
        timeline: list[str] | None = None,
    ) -> None:
        self._results = results or {}
        self._errors = errors or {}
        self._started = started
        self._release = release
        self._timeline = timeline
        self.calls: list[Any] = []
        self.completed = False
        self.cancelled_error: asyncio.CancelledError | None = None

    @property
    def name(self) -> str:
        return "external_search"

    async def invoke(self, input: Any) -> list[ExternalSearchCandidate]:
        self.calls.append(input)
        try:
            if self._started is not None:
                self._started.set()
            if self._release is not None:
                await self._release.wait()
            if input.query in self._errors:
                raise self._errors[input.query]
            self.completed = True
            return list(self._results.get(input.query, []))
        except asyncio.CancelledError as exc:
            self.cancelled_error = exc
            raise
        finally:
            if self._timeline is not None:
                self._timeline.append("external.finished")


class _BlockingQueryRuntime:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.finished = asyncio.Event()
        self.cancelled = False

    async def invoke(
        self, agent: object, input: object, *, attempt_number: int
    ) -> object:
        del agent, input, attempt_number
        self.started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        finally:
            self.finished.set()


class _ControlledQueryRuntime:
    def __init__(
        self,
        *,
        result: object | None = None,
        error: BaseException | None = None,
        release: asyncio.Event | None = None,
        raised: asyncio.Event | None = None,
        timeline: list[str] | None = None,
    ) -> None:
        self._result = result if result is not None else _query_draft()
        self._error = error
        self._release = release
        self._raised = raised
        self._timeline = timeline
        self.started = asyncio.Event()
        self.finished = asyncio.Event()
        self.completed = False
        self.cancelled_error: asyncio.CancelledError | None = None

    async def invoke(
        self, agent: object, input: object, *, attempt_number: int
    ) -> object:
        del agent, input, attempt_number
        self.started.set()
        try:
            if self._release is not None:
                await self._release.wait()
            if self._error is not None:
                if self._raised is not None:
                    self._raised.set()
                raise self._error
            self.completed = True
            return self._result
        except asyncio.CancelledError as exc:
            self.cancelled_error = exc
            raise
        finally:
            if self._timeline is not None:
                self._timeline.append("external.finished")
            self.finished.set()


class _TaskFailureAfterSiblingStartsRuntime:
    def __init__(self, *, error: BaseException, timeline: list[str]) -> None:
        self._error = error
        self._timeline = timeline
        self.sibling_started = asyncio.Event()
        self.sibling_finished = asyncio.Event()
        self.sibling_cancelled = False

    async def invoke(self, agent: object, input: Any, *, attempt_number: int) -> object:
        del agent, attempt_number
        if input.task.collection_goal == "failing":
            await self.sibling_started.wait()
            raise self._error
        self.sibling_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.sibling_cancelled = True
            raise
        finally:
            self._timeline.append("sibling.finished")
            self.sibling_finished.set()


class _Scope(AbstractAsyncContextManager[ExternalResearchRuntime]):
    def __init__(
        self,
        runtime: ExternalResearchRuntime,
        timeline: list[str],
        *,
        exit_error: BaseException | None = None,
        exit_reached: asyncio.Event | None = None,
    ) -> None:
        self._runtime = runtime
        self._timeline = timeline
        self._exit_error = exit_error
        self._exit_reached = exit_reached
        self.exited = asyncio.Event()
        self.exit_calls = 0
        self.close_succeeded = False
        self.body_exception: BaseException | None = None

    async def __aenter__(self) -> ExternalResearchRuntime:
        self._timeline.append("scope.enter")
        return self._runtime

    async def __aexit__(
        self,
        exc_type: object,
        exc: BaseException | None,
        traceback: object,
    ) -> bool:
        del exc_type, traceback
        self.exit_calls += 1
        self.body_exception = exc
        self._timeline.append("scope.exit")
        self.exited.set()
        if self._exit_reached is not None:
            self._exit_reached.set()
        if self._exit_error is not None:
            raise self._exit_error
        self.close_succeeded = True
        return False


class _Factory:
    def __init__(
        self,
        runtimes: list[ExternalResearchRuntime],
        timeline: list[str],
        *,
        activation_error: BaseException | None = None,
        activation_reached: asyncio.Event | None = None,
        exit_error: BaseException | None = None,
        exit_reached: asyncio.Event | None = None,
    ) -> None:
        self._runtimes = runtimes
        self._timeline = timeline
        self.scopes: list[_Scope] = []
        self.activated = asyncio.Event()
        self._activation_error = activation_error
        self._activation_reached = activation_reached
        self._exit_error = exit_error
        self._exit_reached = exit_reached
        self.activate_calls = 0

    def activate(self) -> _Scope:
        self.activate_calls += 1
        if self._activation_reached is not None:
            self._activation_reached.set()
        if self._activation_error is not None:
            raise self._activation_error
        scope = _Scope(
            self._runtimes.pop(0),
            self._timeline,
            exit_error=self._exit_error,
            exit_reached=self._exit_reached,
        )
        self.scopes.append(scope)
        self.activated.set()
        return scope


class _EvidenceAnswerer:
    def __init__(
        self, *, error: BaseException | None = None, timeline: list[str]
    ) -> None:
        self._error = error
        self._timeline = timeline
        self.calls: list[list[Any]] = []

    async def answer(
        self,
        *,
        request: AnsweringRequest,
        evidence: list[object],
        target_time_window: TargetTimeWindow | None,
    ) -> EvidenceAnswerDraft:
        del request, target_time_window
        self._timeline.append("answerer.start")
        self.calls.append(list(evidence))
        if self._error is not None:
            raise self._error
        return EvidenceAnswerDraft(
            sufficiency="insufficient",
            answer="根拠が不足しています。",
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


class _UnreachableDirectAnswerer:
    async def answer(
        self, *, request: AnsweringRequest, previous_answer: str = ""
    ) -> DirectAnswerDraft:
        raise AssertionError(
            f"direct answer must not run: {request!r} {previous_answer!r}"
        )


def _runtime(
    query_runtime: object,
    *,
    selector_runtime: object | None = None,
    tool: _Tool | None = None,
) -> ExternalResearchRuntime:
    return ExternalResearchRuntime(
        query_runtime=query_runtime,  # type: ignore[arg-type]
        selector_runtime=(selector_runtime or ScriptedAgentRuntime([])),  # type: ignore[arg-type]
        search_tool=(tool or _Tool()),  # type: ignore[arg-type]
    )


def _runner(
    *,
    plan: QuestionPlan,
    internal: _InternalSearch,
    factory: _Factory,
    timeline: list[str],
    answer_error: BaseException | None = None,
    planner_error: BaseException | None = None,
    progress: _Progress | None = None,
    events: object | None = None,
    requested_agent_count: int | None = None,
) -> AnsweringRunner:
    phases = AnsweringPhases(
        planner=_Planner(plan, error=planner_error),
        internal_search=internal,
        external_runtime_factory=factory,
        direct_answerer=_UnreachableDirectAnswerer(),
        evidence_answerer=_EvidenceAnswerer(error=answer_error, timeline=timeline),
    )
    return AnsweringRunner(
        context_preparer=_Preparer(),
        phases_factory=lambda: phases,
        progress=progress,
        events=events,  # type: ignore[arg-type]
        requested_external_agent_count=requested_agent_count,
    )


async def _run(runner: AnsweringRunner) -> None:
    await runner.run(
        RunInput(question="NVIDIA の見通しは？", history=()),
        run_context=RUN_CONTEXT,
    )


def _capture_external_outcome(
    monkeypatch: pytest.MonkeyPatch,
) -> list[Any]:
    captured: list[Any] = []
    original = answering_runner_module.normalize_answer_evidence

    def capture(outcome: Any) -> Any:
        captured.append(outcome)
        return original(outcome)

    monkeypatch.setattr(answering_runner_module, "normalize_answer_evidence", capture)
    return captured


def _external_plan(*tasks: ExternalResearchTask) -> ExternalSearchPlan:
    return ExternalSearchPlan(
        external_research_tasks=list(tasks or (_task(),)),
        target_time_window=_TARGET_TIME_WINDOW,
        reason="external",
    )


def _mixed_plan(*tasks: ExternalResearchTask) -> InternalAndExternalPlan:
    return InternalAndExternalPlan(
        internal_queries=["NVIDIA", "Blackwell"],
        external_research_tasks=list(tasks or (_task(),)),
        target_time_window=_TARGET_TIME_WINDOW,
        reason="mixed",
    )


@pytest.mark.asyncio
async def test_external_scope_exits_before_evidence_answering_starts() -> None:
    timeline: list[str] = []
    factory = _Factory([_runtime(ScriptedAgentRuntime([_query_draft()]))], timeline)
    runner = _runner(
        plan=ExternalSearchPlan(
            external_research_tasks=[_task()],
            target_time_window=_TARGET_TIME_WINDOW,
            reason="external",
        ),
        internal=_InternalSearch(),
        factory=factory,
        timeline=timeline,
    )

    await _run(runner)

    assert (
        factory.scopes[0].exit_calls,
        timeline.index("scope.exit") < timeline.index("answerer.start"),
    ) == (1, True)


@pytest.mark.asyncio
async def test_mixed_external_scope_closes_while_internal_branch_is_pending() -> None:
    timeline: list[str] = []
    release_internal = asyncio.Event()
    internal = _InternalSearch(release=release_internal)
    factory = _Factory([_runtime(ScriptedAgentRuntime([_query_draft()]))], timeline)
    runner = _runner(
        plan=InternalAndExternalPlan(
            internal_queries=["NVIDIA"],
            external_research_tasks=[_task()],
            target_time_window=_TARGET_TIME_WINDOW,
            reason="mixed",
        ),
        internal=internal,
        factory=factory,
        timeline=timeline,
    )
    running = asyncio.create_task(_run(runner))

    try:
        await asyncio.wait_for(factory.activated.wait(), timeout=0.5)
        await asyncio.wait_for(factory.scopes[0].exited.wait(), timeout=0.5)
        assert (
            internal.started.is_set(),
            internal.finished.is_set(),
            running.done(),
        ) == (
            True,
            False,
            False,
        )
    finally:
        release_internal.set()
        await asyncio.wait_for(running, timeout=0.5)


@pytest.mark.asyncio
async def test_answer_failure_closes_fresh_external_scope_each_run() -> None:
    timeline: list[str] = []
    factory = _Factory(
        [
            _runtime(ScriptedAgentRuntime([_query_draft()])),
            _runtime(ScriptedAgentRuntime([_query_draft()])),
        ],
        timeline,
    )
    error = RuntimeError("answer failure")
    runner = _runner(
        plan=ExternalSearchPlan(
            external_research_tasks=[_task()],
            target_time_window=None,
            reason="external",
        ),
        internal=_InternalSearch(),
        factory=factory,
        timeline=timeline,
        answer_error=error,
    )

    with pytest.raises(RuntimeError) as raised:
        await _run(runner)
    with pytest.raises(RuntimeError):
        await _run(runner)

    assert (
        raised.value is error,
        len(factory.scopes),
        factory.scopes[0] is not factory.scopes[1],
        [scope.exit_calls for scope in factory.scopes],
        [
            timeline.index("scope.exit", start)
            < timeline.index("answerer.start", start)
            for start in (0, timeline.index("answerer.start") + 1)
        ],
    ) == (True, 2, True, [1, 1], [True, True])


@pytest.mark.asyncio
async def test_outer_cancellation_joins_external_query_before_scope_close() -> None:
    timeline: list[str] = []
    query_runtime = _BlockingQueryRuntime()
    factory = _Factory([_runtime(query_runtime)], timeline)
    runner = _runner(
        plan=ExternalSearchPlan(
            external_research_tasks=[_task()],
            target_time_window=None,
            reason="external",
        ),
        internal=_InternalSearch(),
        factory=factory,
        timeline=timeline,
    )
    running = asyncio.create_task(_run(runner))

    await asyncio.wait_for(query_runtime.started.wait(), timeout=0.5)
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(running, timeout=0.5)

    assert (
        query_runtime.cancelled,
        query_runtime.finished.is_set(),
        factory.scopes[0].exit_calls,
    ) == (True, True, 1)


@pytest.mark.asyncio
async def test_internal_plan_never_activates_external_scope_or_time_filter_metric(
    capfire: CaptureLogfire,
) -> None:
    timeline: list[str] = []
    factory = _Factory([], timeline)
    runner = _runner(
        plan=InternalRetrievalPlan(
            internal_queries=["NVIDIA"],
            reason="internal",
        ),
        internal=_InternalSearch(),
        factory=factory,
        timeline=timeline,
    )

    await _run(runner)
    metrics = collected_metrics(capfire)

    assert (
        factory.scopes,
        [
            metric
            for metric in metrics
            if metric["name"] == "external_search_time_filter_resolution_total"
        ],
    ) == ([], [])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("plan", "expect_internal", "expect_external"),
    [
        pytest.param(
            InternalRetrievalPlan(internal_queries=["NVIDIA"], reason="internal"),
            1,
            0,
            id="internal",
        ),
        pytest.param(_external_plan(), 0, 1, id="external"),
        pytest.param(_mixed_plan(), 1, 1, id="mixed"),
    ],
)
async def test_runner_selects_only_retrieval_dependencies_for_plan_variant(
    plan: QuestionPlan,
    expect_internal: int,
    expect_external: int,
) -> None:
    timeline: list[str] = []
    internal = _InternalSearch()
    factory = _Factory(
        [_runtime(ScriptedAgentRuntime([_query_draft()]))] if expect_external else [],
        timeline,
    )

    await _run(
        _runner(plan=plan, internal=internal, factory=factory, timeline=timeline)
    )

    assert (len(internal.calls), len(factory.scopes)) == (
        expect_internal,
        expect_external,
    )


@pytest.mark.asyncio
async def test_runner_preserves_internal_query_order() -> None:
    timeline: list[str] = []
    internal = _InternalSearch()
    runner = _runner(
        plan=InternalRetrievalPlan(
            internal_queries=["NVIDIA", "nvidia", "OpenAI"],
            reason="internal",
        ),
        internal=internal,
        factory=_Factory([], timeline),
        timeline=timeline,
    )

    await _run(runner)

    assert internal.calls == [
        InternalSearchQueries(queries=("NVIDIA", "nvidia", "OpenAI"))
    ]


@pytest.mark.asyncio
async def test_runner_preserves_internal_hit_order_into_synthesis() -> None:
    timeline: list[str] = []
    answerer = _EvidenceAnswerer(timeline=timeline)
    phases = AnsweringPhases(
        planner=_Planner(
            InternalRetrievalPlan(internal_queries=["NVIDIA"], reason="internal")
        ),
        internal_search=_InternalSearch(
            hits=[
                _hit(assessment_id=1001, title="first"),
                _hit(assessment_id=1002, title="second"),
            ]
        ),
        external_runtime_factory=_Factory([], timeline),
        direct_answerer=_UnreachableDirectAnswerer(),
        evidence_answerer=answerer,
    )
    runner = AnsweringRunner(
        context_preparer=_Preparer(),
        phases_factory=lambda: phases,
        events=None,
        requested_external_agent_count=None,
    )

    await _run(runner)

    assert [item.source.title for item in answerer.calls[0]] == ["first", "second"]


@pytest.mark.asyncio
async def test_runner_passes_external_plan_values_to_query_input(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    timeline: list[str] = []
    task = _task("verify typed input")
    runtime = ScriptedAgentRuntime([_query_draft()])
    captured = _capture_external_outcome(monkeypatch)
    runner = _runner(
        plan=_external_plan(task),
        internal=_InternalSearch(),
        factory=_Factory([_runtime(runtime)], timeline),
        timeline=timeline,
    )

    await _run(runner)

    query_input = runtime.calls[0].input
    assert (
        query_input.task,
        query_input.as_of,
        query_input.target_time_window,
        captured[0].external_search.tasks,
    ) == (task, RUN_CONTEXT.as_of, _TARGET_TIME_WINDOW, [task])


@pytest.mark.asyncio
async def test_scope_exits_after_unclassified_task_sibling_joins() -> None:
    error = RuntimeError("task failure")
    timeline: list[str] = []
    runtime = _TaskFailureAfterSiblingStartsRuntime(error=error, timeline=timeline)
    factory = _Factory([_runtime(runtime)], timeline)
    runner = _runner(
        plan=_external_plan(_task("failing"), _task("blocking")),
        internal=_InternalSearch(),
        factory=factory,
        timeline=timeline,
    )

    with pytest.raises(RuntimeError) as raised:
        await asyncio.wait_for(_run(runner), timeout=0.5)

    assert (
        raised.value is error,
        runtime.sibling_cancelled,
        runtime.sibling_finished.is_set(),
        factory.scopes[0].exited.is_set(),
        timeline.index("sibling.finished") < timeline.index("scope.exit"),
    ) == (True, True, True, True, True)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_stage", ["planning", "retrieving"])
async def test_pre_dispatch_failure_does_not_activate_external_scope(
    failure_stage: str,
) -> None:
    error = RuntimeError(f"{failure_stage} failure")
    timeline: list[str] = []
    factory = _Factory([_runtime(ScriptedAgentRuntime([_query_draft()]))], timeline)
    progress = (
        _Progress(timeline, error_stage="retrieving", error=error)
        if failure_stage == "retrieving"
        else None
    )
    runner = _runner(
        plan=_external_plan(),
        internal=_InternalSearch(),
        factory=factory,
        timeline=timeline,
        planner_error=error if failure_stage == "planning" else None,
        progress=progress,
    )

    with pytest.raises(RuntimeError) as raised:
        await _run(runner)

    assert (raised.value is error, factory.activate_calls, factory.scopes) == (
        True,
        0,
        [],
    )


@pytest.mark.asyncio
async def test_classified_external_failure_is_an_outcome_and_scope_closes_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.agent.runtime.contract import (
        AgentResponseDefect,
        AgentResponseInvalidError,
    )

    timeline: list[str] = []
    captured = _capture_external_outcome(monkeypatch)
    factory = _Factory(
        [
            _runtime(
                ScriptedAgentRuntime(
                    [AgentResponseInvalidError(AgentResponseDefect.RESPONSE_NOT_JSON)]
                )
            )
        ],
        timeline,
    )
    runner = _runner(
        plan=_external_plan(),
        internal=_InternalSearch(),
        factory=factory,
        timeline=timeline,
    )

    await _run(runner)

    assert (
        captured[0].external_search.task_reports[0].status,
        factory.scopes[0].exit_calls,
        factory.scopes[0].close_succeeded,
    ) == ("query_generation_failed", 1, True)


@pytest.mark.asyncio
async def test_external_unknown_error_closes_scope_before_identity_propagation() -> (
    None
):
    error = RuntimeError("external unknown")
    timeline: list[str] = []
    factory = _Factory([_runtime(ScriptedAgentRuntime([error]))], timeline)
    runner = _runner(
        plan=_external_plan(),
        internal=_InternalSearch(),
        factory=factory,
        timeline=timeline,
    )

    with pytest.raises(RuntimeError) as raised:
        await _run(runner)

    assert (
        raised.value is error,
        factory.scopes[0].exit_calls,
        factory.scopes[0].body_exception is error,
    ) == (True, 1, True)


@pytest.mark.asyncio
async def test_mixed_retrieval_starts_internal_and_external_branches_concurrently() -> (
    None
):
    timeline: list[str] = []
    internal_release = asyncio.Event()
    internal = _InternalSearch(release=internal_release)
    query = _BlockingQueryRuntime()
    factory = _Factory([_runtime(query)], timeline)
    runner = _runner(
        plan=_mixed_plan(),
        internal=internal,
        factory=factory,
        timeline=timeline,
    )
    running = asyncio.create_task(_run(runner))

    try:
        await asyncio.wait_for(internal.started.wait(), timeout=0.5)
        await asyncio.wait_for(query.started.wait(), timeout=0.5)
        assert running.done() is False
    finally:
        internal_release.set()
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(running, timeout=0.5)


@pytest.mark.asyncio
async def test_runner_converts_only_internal_search_error_to_failure_value() -> None:
    timeline: list[str] = []
    runner = _runner(
        plan=InternalRetrievalPlan(internal_queries=["NVIDIA"], reason="internal"),
        internal=_InternalSearch(error=InternalSearchError(phase="article_search")),
        factory=_Factory([], timeline),
        timeline=timeline,
    )

    result = await runner.run(
        RunInput(question="NVIDIA の見通しは？", history=()),
        run_context=RUN_CONTEXT,
    )

    assert result.final_output.retrieval.collection_failures == ["internal_search"]


@pytest.mark.asyncio
async def test_mixed_classified_internal_failure_keeps_external_outcome(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    timeline: list[str] = []
    captured = _capture_external_outcome(monkeypatch)
    query = ScriptedAgentRuntime([_query_draft()])
    runner = _runner(
        plan=_mixed_plan(),
        internal=_InternalSearch(error=InternalSearchError(phase="article_search")),
        factory=_Factory([_runtime(query)], timeline),
        timeline=timeline,
    )

    result = await runner.run(
        RunInput(question="NVIDIA の見通しは？", history=()),
        run_context=RUN_CONTEXT,
    )

    assert (
        result.final_output.retrieval.collection_failures,
        captured[0].external_search.task_reports[0].status,
    ) == (["internal_search"], "succeeded")


@pytest.mark.asyncio
async def test_zero_internal_hits_remain_successful() -> None:
    timeline: list[str] = []
    result = await _runner(
        plan=InternalRetrievalPlan(internal_queries=["NVIDIA"], reason="internal"),
        internal=_InternalSearch(),
        factory=_Factory([], timeline),
        timeline=timeline,
    ).run(
        RunInput(question="NVIDIA の見通しは？", history=()),
        run_context=RUN_CONTEXT,
    )

    assert result.final_output.retrieval.collection_failures == []


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["internal", "external"])
async def test_unknown_single_branch_error_propagates_by_identity(mode: str) -> None:
    error = RuntimeError(f"{mode} unknown")
    timeline: list[str] = []
    plan: QuestionPlan = (
        InternalRetrievalPlan(internal_queries=["NVIDIA"], reason="internal")
        if mode == "internal"
        else _external_plan()
    )
    runner = _runner(
        plan=plan,
        internal=_InternalSearch(error=error if mode == "internal" else None),
        factory=_Factory(
            [_runtime(ScriptedAgentRuntime([error]))] if mode == "external" else [],
            timeline,
        ),
        timeline=timeline,
    )

    with pytest.raises(RuntimeError) as raised:
        await _run(runner)

    assert raised.value is error


@pytest.mark.asyncio
async def test_mixed_waits_for_internal_before_external_error() -> None:
    error = RuntimeError("external unknown")
    timeline: list[str] = []
    internal_release = asyncio.Event()
    external_raised = asyncio.Event()
    internal = _InternalSearch(release=internal_release)
    external = _ControlledQueryRuntime(
        error=error,
        release=internal.started,
        raised=external_raised,
    )
    runner = _runner(
        plan=_mixed_plan(),
        internal=internal,
        factory=_Factory([_runtime(external)], timeline),
        timeline=timeline,
    )
    running = asyncio.create_task(_run(runner))

    await asyncio.wait_for(external_raised.wait(), timeout=0.5)
    assert running.done() is False
    internal_release.set()
    with pytest.raises(RuntimeError) as raised:
        await asyncio.wait_for(running, timeout=0.5)

    assert (raised.value is error, internal.completed) == (True, True)


@pytest.mark.asyncio
async def test_mixed_waits_for_external_before_internal_error() -> None:
    error = RuntimeError("internal unknown")
    timeline: list[str] = []
    external_release = asyncio.Event()
    internal_raised = asyncio.Event()
    external = _ControlledQueryRuntime(release=external_release)
    internal = _InternalSearch(
        error=error,
        release=external.started,
        raised=internal_raised,
    )
    runner = _runner(
        plan=_mixed_plan(),
        internal=internal,
        factory=_Factory([_runtime(external)], timeline),
        timeline=timeline,
    )
    running = asyncio.create_task(_run(runner))

    await asyncio.wait_for(internal_raised.wait(), timeout=0.5)
    assert running.done() is False
    external_release.set()
    with pytest.raises(RuntimeError) as raised:
        await asyncio.wait_for(running, timeout=0.5)

    assert (raised.value is error, external.completed) == (True, True)


@pytest.mark.asyncio
async def test_mixed_run_prefers_internal_error_after_both_branches_finish() -> None:
    internal_error = RuntimeError("internal unknown")
    external_error = RuntimeError("external unknown")
    timeline: list[str] = []
    external = _ControlledQueryRuntime(error=external_error)
    internal = _InternalSearch(error=internal_error, release=external.started)
    runner = _runner(
        plan=_mixed_plan(),
        internal=internal,
        factory=_Factory([_runtime(external)], timeline),
        timeline=timeline,
    )

    with pytest.raises(RuntimeError) as raised:
        await asyncio.wait_for(_run(runner), timeout=0.5)

    assert raised.value is internal_error


@pytest.mark.asyncio
async def test_mixed_activation_failure_waits_for_internal_before_propagation() -> None:
    activation_error = RuntimeError("external activation failure")
    activation_reached = asyncio.Event()
    internal_release = asyncio.Event()
    timeline: list[str] = []
    internal = _InternalSearch(release=internal_release)
    factory = _Factory(
        [],
        timeline,
        activation_error=activation_error,
        activation_reached=activation_reached,
    )
    runner = _runner(
        plan=_mixed_plan(),
        internal=internal,
        factory=factory,
        timeline=timeline,
    )
    running = asyncio.create_task(_run(runner))

    await asyncio.wait_for(internal.started.wait(), timeout=0.5)
    await asyncio.wait_for(activation_reached.wait(), timeout=0.5)
    assert running.done() is False
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
    close_error = RuntimeError("external close failure")
    timeline: list[str] = []
    factory = _Factory(
        [_runtime(ScriptedAgentRuntime([_query_draft()]))],
        timeline,
        exit_error=close_error,
    )
    runner = _runner(
        plan=_external_plan(),
        internal=_InternalSearch(),
        factory=factory,
        timeline=timeline,
    )

    with pytest.raises(RuntimeError) as raised:
        await _run(runner)

    scope = factory.scopes[0]
    assert (
        raised.value is close_error,
        scope.exit_calls,
        scope.close_succeeded,
        scope.body_exception,
    ) == (True, 1, False, None)


@pytest.mark.asyncio
async def test_external_only_close_failure_replaces_unknown_body_error() -> None:
    body_error = RuntimeError("external body failure")
    close_error = RuntimeError("external close failure")
    timeline: list[str] = []
    factory = _Factory(
        [_runtime(ScriptedAgentRuntime([body_error]))],
        timeline,
        exit_error=close_error,
    )
    runner = _runner(
        plan=_external_plan(),
        internal=_InternalSearch(),
        factory=factory,
        timeline=timeline,
    )

    with pytest.raises(RuntimeError) as raised:
        await _run(runner)

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
    close_error = RuntimeError("external close failure")
    internal_error = RuntimeError("internal unknown")
    internal_release = asyncio.Event()
    scope_exit_reached = asyncio.Event()
    timeline: list[str] = []
    internal = _InternalSearch(
        error=internal_error if internal_fails else None,
        release=internal_release,
    )
    factory = _Factory(
        [_runtime(ScriptedAgentRuntime([_query_draft()]))],
        timeline,
        exit_error=close_error,
        exit_reached=scope_exit_reached,
    )
    runner = _runner(
        plan=_mixed_plan(),
        internal=internal,
        factory=factory,
        timeline=timeline,
    )
    running = asyncio.create_task(_run(runner))

    await asyncio.wait_for(internal.started.wait(), timeout=0.5)
    await asyncio.wait_for(scope_exit_reached.wait(), timeout=0.5)
    assert running.done() is False
    internal_release.set()
    with pytest.raises(RuntimeError) as raised:
        await asyncio.wait_for(running, timeout=0.5)

    scope = factory.scopes[0]
    assert (
        raised.value is (internal_error if internal_fails else close_error),
        internal.finished.is_set(),
        scope.exit_calls,
        scope.close_succeeded,
        scope.body_exception,
    ) == (True, True, 1, False, None)


@pytest.mark.asyncio
async def test_mixed_outer_cancellation_joins_branches_before_scope_close() -> None:
    timeline: list[str] = []
    internal = _InternalSearch(release=asyncio.Event(), timeline=timeline)
    external = _ControlledQueryRuntime(release=asyncio.Event(), timeline=timeline)
    scope_exit_reached = asyncio.Event()
    factory = _Factory(
        [_runtime(external)],
        timeline,
        exit_reached=scope_exit_reached,
    )
    runner = _runner(
        plan=_mixed_plan(),
        internal=internal,
        factory=factory,
        timeline=timeline,
    )
    running = asyncio.create_task(_run(runner))

    await asyncio.wait_for(internal.started.wait(), timeout=0.5)
    await asyncio.wait_for(external.started.wait(), timeout=0.5)
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
    ) == (True, True, True, True, True, 1, True, True, True)


@pytest.mark.asyncio
@pytest.mark.parametrize("first_failure", ["internal", "external"])
async def test_mixed_outer_cancellation_wins_over_completed_unknown_failure(
    first_failure: str,
) -> None:
    first_error = RuntimeError(f"{first_failure} unknown")
    first_raised = asyncio.Event()
    timeline: list[str] = []
    blocked = asyncio.Event()
    if first_failure == "internal":
        external = _ControlledQueryRuntime(release=blocked, timeline=timeline)
        internal = _InternalSearch(
            error=first_error,
            release=external.started,
            raised=first_raised,
            timeline=timeline,
        )
    else:
        internal = _InternalSearch(release=blocked, timeline=timeline)
        external = _ControlledQueryRuntime(
            error=first_error,
            release=internal.started,
            raised=first_raised,
            timeline=timeline,
        )
    scope_exit_reached = asyncio.Event()
    factory = _Factory(
        [_runtime(external)],
        timeline,
        exit_reached=scope_exit_reached,
    )
    runner = _runner(
        plan=_mixed_plan(),
        internal=internal,
        factory=factory,
        timeline=timeline,
    )
    running = asyncio.create_task(_run(runner))

    await asyncio.wait_for(first_raised.wait(), timeout=0.5)
    assert running.done() is False
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(running, timeout=0.5)

    completed = internal if first_failure == "internal" else external
    cancelled = external if first_failure == "internal" else internal
    assert (
        cancelled.cancelled_error is not None,
        completed.cancelled_error,
        internal.finished.is_set(),
        external.finished.is_set(),
        scope_exit_reached.is_set(),
        factory.scopes[0].exit_calls,
        factory.scopes[0].close_succeeded,
    ) == (True, None, True, True, True, 1, True)


@pytest.mark.asyncio
async def test_external_close_failure_replaces_body_cancellation() -> None:
    close_error = RuntimeError("close failed during cancellation")
    timeline: list[str] = []
    external = _ControlledQueryRuntime(release=asyncio.Event(), timeline=timeline)
    scope_exit_reached = asyncio.Event()
    factory = _Factory(
        [_runtime(external)],
        timeline,
        exit_error=close_error,
        exit_reached=scope_exit_reached,
    )
    runner = _runner(
        plan=_external_plan(),
        internal=_InternalSearch(),
        factory=factory,
        timeline=timeline,
    )
    running = asyncio.create_task(_run(runner))

    await asyncio.wait_for(external.started.wait(), timeout=0.5)
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
    close_error = RuntimeError("close failed during mixed cancellation")
    timeline: list[str] = []
    internal = _InternalSearch(release=asyncio.Event(), timeline=timeline)
    external = _ControlledQueryRuntime(release=asyncio.Event(), timeline=timeline)
    scope_exit_reached = asyncio.Event()
    factory = _Factory(
        [_runtime(external)],
        timeline,
        exit_error=close_error,
        exit_reached=scope_exit_reached,
    )
    runner = _runner(
        plan=_mixed_plan(),
        internal=internal,
        factory=factory,
        timeline=timeline,
    )
    running = asyncio.create_task(_run(runner))

    await asyncio.wait_for(internal.started.wait(), timeout=0.5)
    await asyncio.wait_for(external.started.wait(), timeout=0.5)
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
        close_error.__context__ is scope.body_exception,
        timeline.index("external.finished") < timeline.index("scope.exit"),
    ) == (True, True, True, True, True, 1, False, True, True)
