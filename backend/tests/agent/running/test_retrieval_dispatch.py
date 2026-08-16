"""AnsweringRunner の retrieval dispatch と external resource scope 契約。"""

from __future__ import annotations

import asyncio
import json
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pytest
from logfire.testing import CaptureLogfire

from app.agent.answering.contract import AnsweringRequest
from app.agent.answering.direct_answer.contract import DirectAnswerDraft
from app.agent.answering.evidence_answer.contract import (
    EvidenceAnswerOutcome,
    EvidenceAnswerUnavailable,
)
from app.agent.contract import AnswerProgressStage
from app.agent.evidence_collection import CollectedNews, NewsCollector, Researcher
from app.agent.evidence_collection.external_search import (
    ExternalResearchRuntime,
    ExternalSearchCandidate,
    ExternalSearchProviderError,
)
from app.agent.evidence_collection.internal_search import (
    InternalArticleContent,
    InternalArticleSearchHit,
)
from app.agent.evidence_collection.internal_search.contract import InternalSearchError
from app.agent.evidence_collection.internal_search.query_embedding import (
    InternalSearchQueries,
)
from app.agent.evidence_review import (
    EvidenceReviewer,
    EvidenceRunCompleted,
    EvidenceRunResult,
)
from app.agent.planning.contract import (
    ExternalResearchTask,
    PlanningRequest,
    QuestionPlan,
    ResearchTask,
    SearchPlan,
    TargetTimeWindow,
)
from app.agent.question_context import QuestionContext
from app.agent.running import AnsweringPhases, AnsweringRunner, RunContext, RunInput
from app.agent.running import answering_runner as answering_runner_module
from app.analysis.analyzed_article import InScopeAnalyzedArticle
from app.analysis.assessment.domain.result import InScope, InScopeCategory
from tests.agent.running._input_safety import AllowInputSafetyChecker
from tests.agent.runtime._fakes import ScriptedAgentRuntime
from tests.logfire._metric_helpers import collected_metrics

RUN_CONTEXT = RunContext(
    run_id=UUID("019bd239-1ed4-7fbb-a336-04fe3c197652"),
    as_of=datetime(2026, 7, 20, 9, 30, tzinfo=UTC),
)
_TARGET_TIME_WINDOW = TargetTimeWindow(kind="last_n_days", days=1)


def _task(goal: str = "NVIDIA の供給を確認する") -> ExternalResearchTask:
    return ExternalResearchTask(research_goal=goal)


def _query_draft() -> Any:
    from app.agent.evidence_collection.external_search.contract import (
        ExternalQueryDraft,
    )

    return ExternalQueryDraft(queries=["NVIDIA supply"])


def _review_draft_selecting(indexes: list[int]) -> Any:
    """D4-S1: 統合index空間の指定indexを採用するreviewer draft。

    候補数より大きいindexは範囲外dropとなるだけで安全なため、実際の候補数を
    問わず[0, 1]等を渡して「提示された候補を全て採用させる」用途に使える。
    """
    from app.agent.evidence_review import EvidenceReviewerDraft

    return EvidenceReviewerDraft.model_validate(
        {
            "selections": [
                {
                    "option_index": index,
                    "claim": f"claim-{index}",
                    "why_selected": "w",
                }
                for index in indexes
            ],
            "missing": [],
        }
    )


def _review_draft_selecting_with_missing(indexes: list[int], missing: list[str]) -> Any:
    """条件7用: 採用indexに加えてRun単位のmissingを申告するreviewer draft。"""
    from app.agent.evidence_review import EvidenceReviewerDraft

    return EvidenceReviewerDraft.model_validate(
        {
            "selections": [
                {
                    "option_index": index,
                    "claim": f"claim-{index}",
                    "why_selected": "w",
                }
                for index in indexes
            ],
            "missing": missing,
        }
    )


def _candidate(url: str, *, title: str) -> ExternalSearchCandidate:
    return ExternalSearchCandidate(url=url, title=title, snippet="snippet")


def _hit(
    *,
    assessment_id: int,
    title: str,
    curation_id: int | None = None,
    distance: float = 0.1,
) -> InternalArticleSearchHit:
    article = InScopeAnalyzedArticle(
        curation_id=curation_id if curation_id is not None else assessment_id - 1000,
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
        distance=distance,
    )


class _Preparer:
    async def prepare(self, **_kwargs: object) -> QuestionContext:
        return QuestionContext(standalone_question="NVIDIA の見通しは？")


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

    @property
    def name(self) -> str:
        return "internal_search"

    async def search(self, input: Any) -> list[InternalArticleSearchHit]:
        self.calls.append(input.queries)
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
        self.finished = asyncio.Event()

    @property
    def name(self) -> str:
        return "external_search"

    async def search(self, input: Any) -> list[ExternalSearchCandidate]:
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
            self.finished.set()


class _BlockingQueryRuntime:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.finished = asyncio.Event()
        self.cancelled = False

    async def call(
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

    async def call(
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

    async def call(self, agent: object, input: Any, *, attempt_number: int) -> object:
        del agent, attempt_number
        if input.task.research_goal == "failing":
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
        self.review_missing_calls: list[tuple[str, ...]] = []

    async def answer(
        self,
        *,
        request: AnsweringRequest,
        evidence: list[object],
        target_time_window: TargetTimeWindow | None,
        review_missing: tuple[str, ...] = (),
    ) -> EvidenceAnswerOutcome:
        del request, target_time_window
        self._timeline.append("answerer.start")
        self.calls.append(list(evidence))
        self.review_missing_calls.append(review_missing)
        if self._error is not None:
            raise self._error
        # このfakeはevidenceの内容を問わず「回答を作れなかった」を演じる
        # (呼び出し側のtestはevidenceがanswererへ届くことだけを検証する)。
        return EvidenceAnswerUnavailable(failure_code="fake_evidence_unavailable")


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


class _Events:
    def __init__(self) -> None:
        self.events: list[Any] = []

    async def event_occurred(self, event: Any) -> None:
        self.events.append(event)


def _internal_search_events(events: list[Any]) -> list[Any]:
    """同じreporterへ並走発火するexternal_search.*を除き内部枝のeventだけ残す。"""

    return [
        event
        for event in events
        if event.type.startswith("evidence_collection.internal_search_")
    ]


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
    reviewer_runtime: object | None = None,
    tool: _Tool | None = None,
) -> ExternalResearchRuntime:
    return ExternalResearchRuntime(
        query_runtime=query_runtime,  # type: ignore[arg-type]
        reviewer_runtime=(reviewer_runtime or ScriptedAgentRuntime([])),  # type: ignore[arg-type]
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
        collector=NewsCollector(
            researcher=Researcher(internal_search=internal, events=events),
            requested_agent_count=requested_agent_count,
        ),
        external_runtime_factory=factory,
        direct_answerer=_UnreachableDirectAnswerer(),
        evidence_answerer=_EvidenceAnswerer(error=answer_error, timeline=timeline),
        reviewer=EvidenceReviewer(),
    )
    return AnsweringRunner(
        input_safety_checker=AllowInputSafetyChecker(),
        context_preparer=_Preparer(),
        phases_factory=lambda: phases,
        progress=progress,
        events=events,  # type: ignore[arg-type]
    )


async def _run(runner: AnsweringRunner) -> None:
    await runner.run(
        RunInput(question="NVIDIA の見通しは？", history=()),
        run_context=RUN_CONTEXT,
    )


@dataclass(frozen=True, slots=True)
class _CapturedEvidenceAssemblyInput:
    collected_news: CollectedNews
    evidence_run: EvidenceRunResult


def _task_reports(captured: _CapturedEvidenceAssemblyInput) -> list[Any]:
    return [task.report for task in captured.collected_news.tasks]


def _completed_evidence_run(
    captured: _CapturedEvidenceAssemblyInput,
) -> EvidenceRunCompleted:
    evidence_run = captured.evidence_run
    assert isinstance(evidence_run, EvidenceRunCompleted)
    return evidence_run


def _capture_external_outcome(
    monkeypatch: pytest.MonkeyPatch,
) -> list[_CapturedEvidenceAssemblyInput]:
    captured: list[_CapturedEvidenceAssemblyInput] = []
    original = answering_runner_module.assemble_evidence_result

    def capture(**kwargs: Any) -> Any:
        captured.append(
            _CapturedEvidenceAssemblyInput(
                collected_news=kwargs["collected_news"],
                evidence_run=kwargs["evidence_run"],
            )
        )
        return original(**kwargs)

    monkeypatch.setattr(answering_runner_module, "assemble_evidence_result", capture)
    return captured


def _search_plan(
    *tasks: ExternalResearchTask,
    article_search_queries: list[str] | None = None,
    target_time_window: TargetTimeWindow | None = _TARGET_TIME_WINDOW,
) -> SearchPlan:
    """先頭taskへ全query、追加taskへ1件ずつ割り当てる(合計は予算内)。

    article_search_queriesはtask単位ではなくrun全体の検索文だったため、
    最も情報量の多い先頭queryのtaskへ集約し、他taskはgoalだけを保つ。
    """
    resolved_tasks = list(tasks or (_task(),))
    first_task_queries = article_search_queries or ["NVIDIA", "Blackwell"]
    return SearchPlan(
        research_tasks=[
            ResearchTask(
                research_goal=task.research_goal,
                article_search_queries=(
                    first_task_queries
                    if index == 0
                    else [f"{task.research_goal} query"]
                ),
            )
            for index, task in enumerate(resolved_tasks)
        ],
        target_time_window=target_time_window,
    )


def _plan_with_tasks(
    *goals_and_queries: tuple[str, list[str]],
    target_time_window: TargetTimeWindow | None = _TARGET_TIME_WINDOW,
) -> SearchPlan:
    """段3のtask単位収集テスト用に、taskごとのqueryを直接指定してplanを組む。"""
    return SearchPlan(
        research_tasks=[
            ResearchTask(research_goal=goal, article_search_queries=queries)
            for goal, queries in goals_and_queries
        ],
        target_time_window=target_time_window,
    )


@pytest.mark.asyncio
async def test_external_scope_exits_before_evidence_answering_starts() -> None:
    timeline: list[str] = []
    factory = _Factory([_runtime(ScriptedAgentRuntime([_query_draft()]))], timeline)
    runner = _runner(
        plan=_search_plan(),
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
async def test_scope_stays_open_until_pending_internal_branch_settles() -> None:
    """R1: 内部収集はscope内のtask fan-outに含まれるため、
    pending中はexternal scopeが閉じない。
    """
    timeline: list[str] = []
    release_internal = asyncio.Event()
    internal = _InternalSearch(release=release_internal, timeline=timeline)
    tool = _Tool({"NVIDIA supply": []}, timeline=timeline)
    factory = _Factory(
        [_runtime(ScriptedAgentRuntime([_query_draft()]), tool=tool)],
        timeline,
    )
    runner = _runner(
        plan=_search_plan(article_search_queries=["NVIDIA"]),
        internal=internal,
        factory=factory,
        timeline=timeline,
    )
    running = asyncio.create_task(_run(runner))

    try:
        await asyncio.wait_for(internal.started.wait(), timeout=0.5)
        await asyncio.wait_for(tool.finished.wait(), timeout=0.5)
        assert (internal.finished.is_set(), factory.scopes[0].exited.is_set()) == (
            False,
            False,
        )
    finally:
        release_internal.set()
        await asyncio.wait_for(running, timeout=0.5)

    assert timeline.index("internal.finished") < timeline.index("scope.exit")


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
        plan=_search_plan(target_time_window=None),
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
        plan=_search_plan(target_time_window=None),
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
async def test_search_plan_activates_external_scope_and_resolves_time_filter_metric(
    capfire: CaptureLogfire,
) -> None:
    timeline: list[str] = []
    factory = _Factory([_runtime(ScriptedAgentRuntime([_query_draft()]))], timeline)
    runner = _runner(
        plan=_search_plan(article_search_queries=["NVIDIA"]),
        internal=_InternalSearch(),
        factory=factory,
        timeline=timeline,
    )

    await _run(runner)
    metrics = collected_metrics(capfire)

    assert len(factory.scopes) == 1
    assert [
        metric
        for metric in metrics
        if metric["name"] == "external_search_time_filter_resolution_total"
    ]


@pytest.mark.asyncio
async def test_search_plan_selects_both_retrieval_dependencies() -> None:
    plan = _search_plan()
    timeline: list[str] = []
    internal = _InternalSearch()
    factory = _Factory([_runtime(ScriptedAgentRuntime([_query_draft()]))], timeline)

    await _run(
        _runner(plan=plan, internal=internal, factory=factory, timeline=timeline)
    )

    assert (len(internal.calls), len(factory.scopes)) == (1, 1)


@pytest.mark.asyncio
async def test_runner_preserves_internal_query_order() -> None:
    timeline: list[str] = []
    internal = _InternalSearch()
    runner = _runner(
        plan=_search_plan(article_search_queries=["NVIDIA", "OpenAI", "Apple"]),
        internal=internal,
        factory=_Factory([_runtime(ScriptedAgentRuntime([_query_draft()]))], timeline),
        timeline=timeline,
    )

    await _run(runner)

    assert internal.calls == [
        InternalSearchQueries(queries=("NVIDIA", "OpenAI", "Apple"))
    ]


@pytest.mark.asyncio
async def test_runner_preserves_internal_hit_order_into_synthesis() -> None:
    """D4-S1: reviewerが両方の内部候補を採用する前提でindex順を検証する。"""
    timeline: list[str] = []
    answerer = _EvidenceAnswerer(timeline=timeline)
    reviewer_runtime = ScriptedAgentRuntime([_review_draft_selecting([0, 1])])
    phases = AnsweringPhases(
        planner=_Planner(_search_plan(article_search_queries=["NVIDIA"])),
        collector=NewsCollector(
            researcher=Researcher(
                internal_search=_InternalSearch(
                    hits=[
                        _hit(assessment_id=1001, title="first"),
                        _hit(assessment_id=1002, title="second"),
                    ]
                )
            )
        ),
        external_runtime_factory=_Factory(
            [
                _runtime(
                    ScriptedAgentRuntime([_query_draft()]),
                    reviewer_runtime=reviewer_runtime,
                )
            ],
            timeline,
        ),
        direct_answerer=_UnreachableDirectAnswerer(),
        evidence_answerer=answerer,
        reviewer=EvidenceReviewer(),
    )
    runner = AnsweringRunner(
        input_safety_checker=AllowInputSafetyChecker(),
        context_preparer=_Preparer(),
        phases_factory=lambda: phases,
        events=None,
    )

    await _run(runner)

    assert [item.source.title for item in answerer.calls[0]] == ["first", "second"]


@pytest.mark.asyncio
async def test_runner_forwards_review_missing_to_the_evidence_answerer() -> None:
    """条件7: AnsweringRunnerがoutcome.review.missingを回答Agentへ渡す。

    受け渡しの責務はrunnerにあり、flowがEvidenceCollectionOutcomeから
    自分で取り出す形にしない。reviewerが申告したmissingが、そのまま
    tuple(outcome.review.missing)としてevidence_answererへ渡ることを確認する。
    """
    timeline: list[str] = []
    answerer = _EvidenceAnswerer(timeline=timeline)
    reviewer_runtime = ScriptedAgentRuntime(
        [_review_draft_selecting_with_missing([0], ["観点Aを確認できませんでした"])]
    )
    phases = AnsweringPhases(
        planner=_Planner(_search_plan(article_search_queries=["NVIDIA"])),
        collector=NewsCollector(
            researcher=Researcher(
                internal_search=_InternalSearch(
                    hits=[_hit(assessment_id=1001, title="first")]
                )
            )
        ),
        external_runtime_factory=_Factory(
            [
                _runtime(
                    ScriptedAgentRuntime([_query_draft()]),
                    reviewer_runtime=reviewer_runtime,
                )
            ],
            timeline,
        ),
        direct_answerer=_UnreachableDirectAnswerer(),
        evidence_answerer=answerer,
        reviewer=EvidenceReviewer(),
    )
    runner = AnsweringRunner(
        input_safety_checker=AllowInputSafetyChecker(),
        context_preparer=_Preparer(),
        phases_factory=lambda: phases,
        events=None,
    )

    await _run(runner)

    assert answerer.review_missing_calls[0] == ("観点Aを確認できませんでした",)


@pytest.mark.asyncio
async def test_runner_passes_search_plan_values_to_query_input() -> None:
    """planのgoal/time windowをquery agentへの実際の入力で検証する。"""
    timeline: list[str] = []
    task = _task("verify typed input")
    runtime = ScriptedAgentRuntime([_query_draft()])
    runner = _runner(
        plan=_search_plan(task),
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
    ) == (task, RUN_CONTEXT.as_of, _TARGET_TIME_WINDOW)


@pytest.mark.asyncio
async def test_scope_exits_after_unclassified_task_sibling_joins() -> None:
    """R3: task内の未分類例外で兄弟taskがcancelされ合流した後にscopeが閉じ、
    元例外がidentityで送出される。
    """
    error = RuntimeError("task failure")
    timeline: list[str] = []
    runtime = _TaskFailureAfterSiblingStartsRuntime(error=error, timeline=timeline)
    factory = _Factory([_runtime(runtime)], timeline)
    runner = _runner(
        plan=_search_plan(_task("failing"), _task("blocking")),
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
@pytest.mark.parametrize("failure_stage", ["planning", "evidence_collection"])
async def test_pre_dispatch_failure_does_not_activate_external_scope(
    failure_stage: str,
) -> None:
    error = RuntimeError(f"{failure_stage} failure")
    timeline: list[str] = []
    factory = _Factory([_runtime(ScriptedAgentRuntime([_query_draft()]))], timeline)
    progress = (
        _Progress(timeline, error_stage="evidence_collection", error=error)
        if failure_stage == "evidence_collection"
        else None
    )
    runner = _runner(
        plan=_search_plan(),
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
        plan=_search_plan(),
        internal=_InternalSearch(),
        factory=factory,
        timeline=timeline,
    )

    await _run(runner)

    assert (
        _task_reports(captured[0])[0].external_collection,
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
        plan=_search_plan(),
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
async def test_search_starts_internal_and_external_branches_concurrently() -> None:
    timeline: list[str] = []
    internal_release = asyncio.Event()
    internal = _InternalSearch(release=internal_release)
    query = _BlockingQueryRuntime()
    factory = _Factory([_runtime(query)], timeline)
    runner = _runner(
        plan=_search_plan(),
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
async def test_search_converts_internal_search_error_to_failed_report_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """D4-S2: run単位のcollection_failuresは廃止され、task reportの

    internal_collection="failed"へ一本化される。
    """
    timeline: list[str] = []
    captured = _capture_external_outcome(monkeypatch)
    runner = _runner(
        plan=_search_plan(article_search_queries=["NVIDIA"]),
        internal=_InternalSearch(error=InternalSearchError(phase="article_search")),
        factory=_Factory([_runtime(ScriptedAgentRuntime([_query_draft()]))], timeline),
        timeline=timeline,
    )

    await runner.run(
        RunInput(question="NVIDIA の見通しは？", history=()),
        run_context=RUN_CONTEXT,
    )

    assert _task_reports(captured[0])[0].internal_collection == "failed"


@pytest.mark.asyncio
async def test_search_classified_internal_failure_keeps_external_outcome(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    timeline: list[str] = []
    captured = _capture_external_outcome(monkeypatch)
    query = ScriptedAgentRuntime([_query_draft()])
    runner = _runner(
        plan=_search_plan(),
        internal=_InternalSearch(error=InternalSearchError(phase="article_search")),
        factory=_Factory([_runtime(query)], timeline),
        timeline=timeline,
    )

    await runner.run(
        RunInput(question="NVIDIA の見通しは？", history=()),
        run_context=RUN_CONTEXT,
    )

    report = _task_reports(captured[0])[0]
    assert (report.internal_collection, report.external_collection) == (
        "failed",
        "succeeded",
    )


@pytest.mark.asyncio
async def test_zero_internal_hits_remain_successful_under_search(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """D4-S2: internal候補ゼロはinternal_collection="failed"にならない

    (InternalSearchErrorだけがfailedを表す)。
    """
    timeline: list[str] = []
    captured = _capture_external_outcome(monkeypatch)
    await _runner(
        plan=_search_plan(article_search_queries=["NVIDIA"]),
        internal=_InternalSearch(),
        factory=_Factory([_runtime(ScriptedAgentRuntime([_query_draft()]))], timeline),
        timeline=timeline,
    ).run(
        RunInput(question="NVIDIA の見通しは？", history=()),
        run_context=RUN_CONTEXT,
    )

    assert _task_reports(captured[0])[0].internal_collection == "succeeded"


@pytest.mark.asyncio
async def test_internal_search_events_are_emitted_per_task_with_task_index() -> None:
    """保証するテスト条件 10。"""
    timeline: list[str] = []
    events = _Events()
    hits_by_call = iter(
        [
            [
                _hit(assessment_id=1001, title="first"),
                _hit(assessment_id=1002, title="second"),
            ],
            [_hit(assessment_id=1003, title="third")],
        ]
    )

    class _PerCallInternalSearch:
        @property
        def name(self) -> str:
            return "internal_search"

        async def search(self, input: Any) -> list[InternalArticleSearchHit]:
            del input
            return next(hits_by_call)

    # S1: reviewerはRun単位1回。統合index空間(仮定: task昇順)ではtask0の2件が
    # 0,1、task1の1件が2になる。task単位で呼ぶ旧経路が残っていても2件目の
    # callがscript枯渇crashにならないよう空draftを1件足す。
    reviewer_runtime = ScriptedAgentRuntime(
        [_review_draft_selecting([0, 1, 2]), _review_draft_selecting([])]
    )
    runner = _runner(
        plan=_search_plan(
            _task("first task"),
            _task("second task"),
            article_search_queries=["NVIDIA", "OpenAI"],
        ),
        internal=_PerCallInternalSearch(),
        factory=_Factory(
            [
                _runtime(
                    ScriptedAgentRuntime([_query_draft(), _query_draft()]),
                    reviewer_runtime=reviewer_runtime,
                )
            ],
            timeline,
        ),
        timeline=timeline,
        events=events,
        requested_agent_count=1,
    )

    await _run(runner)

    internal_events = [
        event.model_dump() for event in _internal_search_events(events.events)
    ]
    assert internal_events == [
        {
            "type": "evidence_collection.internal_search_started",
            "task_index": 0,
            "query_count": 2,
        },
        {
            "type": "evidence_collection.internal_search_completed",
            "task_index": 0,
            "hit_count": 2,
        },
        {
            "type": "evidence_collection.internal_search_started",
            "task_index": 1,
            "query_count": 1,
        },
        {
            "type": "evidence_collection.internal_search_completed",
            "task_index": 1,
            "hit_count": 1,
        },
    ]


@pytest.mark.asyncio
async def test_internal_search_failure_reports_started_without_completed() -> None:
    """保証するテスト条件 11。"""
    timeline: list[str] = []
    events = _Events()
    runner = _runner(
        plan=_search_plan(article_search_queries=["NVIDIA"]),
        internal=_InternalSearch(error=InternalSearchError(phase="article_search")),
        factory=_Factory([_runtime(ScriptedAgentRuntime([_query_draft()]))], timeline),
        timeline=timeline,
        events=events,
    )

    await _run(runner)

    internal_events = [
        event.model_dump() for event in _internal_search_events(events.events)
    ]
    assert internal_events == [
        {
            "type": "evidence_collection.internal_search_started",
            "task_index": 0,
            "query_count": 1,
        }
    ]


@pytest.mark.asyncio
async def test_internal_search_events_do_not_expose_query_text() -> None:
    timeline: list[str] = []
    events = _Events()
    reviewer_runtime = ScriptedAgentRuntime([_review_draft_selecting([0])])
    runner = _runner(
        plan=_search_plan(article_search_queries=["SECRET raw user question"]),
        internal=_InternalSearch(hits=[_hit(assessment_id=1001, title="hit")]),
        factory=_Factory(
            [
                _runtime(
                    ScriptedAgentRuntime([_query_draft()]),
                    reviewer_runtime=reviewer_runtime,
                )
            ],
            timeline,
        ),
        timeline=timeline,
        events=events,
    )

    await _run(runner)

    serialized = json.dumps(
        [event.model_dump(mode="json") for event in events.events],
        ensure_ascii=False,
    )
    assert "SECRET raw user question" not in serialized


@pytest.mark.asyncio
@pytest.mark.parametrize("branch", ["internal", "external"])
async def test_unknown_search_branch_error_propagates_by_identity(branch: str) -> None:
    error = RuntimeError(f"{branch} unknown")
    timeline: list[str] = []
    plan = _search_plan(article_search_queries=["NVIDIA"])
    runner = _runner(
        plan=plan,
        internal=_InternalSearch(error=error if branch == "internal" else None),
        factory=_Factory(
            [
                _runtime(
                    ScriptedAgentRuntime(
                        [error if branch == "external" else _query_draft()]
                    )
                )
            ],
            timeline,
        ),
        timeline=timeline,
    )

    with pytest.raises(RuntimeError) as raised:
        await _run(runner)

    assert raised.value is error


@pytest.mark.asyncio
async def test_search_waits_for_internal_before_external_error() -> None:
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
        plan=_search_plan(),
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
async def test_search_waits_for_external_before_internal_error() -> None:
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
        plan=_search_plan(),
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
async def test_search_run_prefers_internal_error_after_both_branches_finish() -> None:
    internal_error = RuntimeError("internal unknown")
    external_error = RuntimeError("external unknown")
    timeline: list[str] = []
    external = _ControlledQueryRuntime(error=external_error)
    internal = _InternalSearch(error=internal_error, release=external.started)
    runner = _runner(
        plan=_search_plan(),
        internal=internal,
        factory=_Factory([_runtime(external)], timeline),
        timeline=timeline,
    )

    with pytest.raises(RuntimeError) as raised:
        await asyncio.wait_for(_run(runner), timeout=0.5)

    assert raised.value is internal_error


@pytest.mark.asyncio
async def test_search_close_failure_propagates_same_sentinel() -> None:
    close_error = RuntimeError("external close failure")
    timeline: list[str] = []
    factory = _Factory(
        [_runtime(ScriptedAgentRuntime([_query_draft()]))],
        timeline,
        exit_error=close_error,
    )
    runner = _runner(
        plan=_search_plan(),
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
async def test_search_close_failure_replaces_unknown_body_error() -> None:
    body_error = RuntimeError("external body failure")
    close_error = RuntimeError("external close failure")
    timeline: list[str] = []
    factory = _Factory(
        [_runtime(ScriptedAgentRuntime([body_error]))],
        timeline,
        exit_error=close_error,
    )
    runner = _runner(
        plan=_search_plan(),
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
async def test_outer_cancellation_joins_task_before_scope_close_then_raises() -> None:
    """R2: 外側cancelで実行中taskの内部・外部枝がcancelされ合流した後にscopeが閉じ、
    その後で呼び出し元へ届くのと同じCancelledErrorが送出される。
    """
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
        plan=_search_plan(),
        internal=internal,
        factory=factory,
        timeline=timeline,
    )
    running = asyncio.create_task(_run(runner))

    await asyncio.wait_for(internal.started.wait(), timeout=0.5)
    await asyncio.wait_for(external.started.wait(), timeout=0.5)
    running.cancel()
    with pytest.raises(asyncio.CancelledError) as raised:
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
        scope.body_exception is raised.value,
        timeline.index("internal.finished") < timeline.index("scope.exit"),
        timeline.index("external.finished") < timeline.index("scope.exit"),
    ) == (True, True, True, True, True, 1, True, True, True, True)


@pytest.mark.asyncio
@pytest.mark.parametrize("first_failure", ["internal", "external"])
async def test_search_outer_cancellation_wins_over_completed_unknown_failure(
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
        plan=_search_plan(),
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
async def test_scope_closes_after_reviewer_failure_exhausts_attempts() -> None:
    """R4: 選別が2 attempt尽きて失敗しても収集全体は継続し、
    external scopeが正常に解放される
    (収集失敗・想定外例外・cancelの解放経路は他テストが既に覆う)。
    """
    from app.agent.runtime.contract import (
        AgentResponseDefect,
        AgentResponseInvalidError,
    )

    timeline: list[str] = []
    reviewer_error = AgentResponseInvalidError(AgentResponseDefect.RESPONSE_NOT_JSON)
    reviewer_runtime = ScriptedAgentRuntime([reviewer_error, reviewer_error])
    tool = _Tool(
        {"NVIDIA supply": [_candidate("https://example.com/ext", title="ext title")]}
    )
    factory = _Factory(
        [
            _runtime(
                ScriptedAgentRuntime([_query_draft()]),
                reviewer_runtime=reviewer_runtime,
                tool=tool,
            )
        ],
        timeline,
    )
    runner = _runner(
        plan=_search_plan(article_search_queries=["NVIDIA"]),
        internal=_InternalSearch(),
        factory=factory,
        timeline=timeline,
    )

    await _run(runner)

    scope = factory.scopes[0]
    assert (scope.exit_calls, scope.close_succeeded) == (1, True)


class _KeyedFailingInternalSearch:
    """queryごとに成否/hitsを切り替えるtask単位分離検証用fake。"""

    def __init__(
        self,
        *,
        failing_queries: set[str],
        hits_by_query: dict[str, list[InternalArticleSearchHit]] | None = None,
    ) -> None:
        self._failing_queries = failing_queries
        self._hits_by_query = hits_by_query or {}
        self.calls: list[InternalSearchQueries] = []

    @property
    def name(self) -> str:
        return "internal_search"

    async def search(self, input: Any) -> list[InternalArticleSearchHit]:
        self.calls.append(input.queries)
        query = input.queries.queries[0]
        if query in self._failing_queries:
            raise InternalSearchError(phase="article_search")
        return list(self._hits_by_query.get(query, []))


@pytest.mark.asyncio
async def test_internal_failure_still_reaches_reviewer_and_produces_evidence() -> None:
    """保証するテスト条件 1(runner経由)。"""
    timeline: list[str] = []
    events = _Events()
    answerer = _EvidenceAnswerer(timeline=timeline)
    query_runtime = ScriptedAgentRuntime([_query_draft()])
    reviewer_runtime = ScriptedAgentRuntime([_review_draft_selecting([0])])
    tool = _Tool(
        {"NVIDIA supply": [_candidate("https://example.com/ext", title="ext title")]}
    )
    factory = _Factory(
        [_runtime(query_runtime, reviewer_runtime=reviewer_runtime, tool=tool)],
        timeline,
    )
    phases = AnsweringPhases(
        planner=_Planner(_plan_with_tasks(("goal0", ["NVIDIA"]))),
        collector=NewsCollector(
            researcher=Researcher(
                internal_search=_InternalSearch(
                    error=InternalSearchError(phase="article_search")
                ),
                events=events,
            )
        ),
        external_runtime_factory=factory,
        direct_answerer=_UnreachableDirectAnswerer(),
        evidence_answerer=answerer,
        reviewer=EvidenceReviewer(),
    )
    runner = AnsweringRunner(
        input_safety_checker=AllowInputSafetyChecker(),
        context_preparer=_Preparer(),
        phases_factory=lambda: phases,
        events=events,
    )

    await _run(runner)

    internal_events = [event.type for event in _internal_search_events(events.events)]
    assert (
        [item.source.title for item in answerer.calls[0]],
        internal_events,
    ) == (["ext title"], ["evidence_collection.internal_search_started"])


@pytest.mark.asyncio
async def test_external_provider_failure_keeps_internal_hits_in_final_evidence() -> (
    None
):
    """保証するテスト条件 2(runner経由)。

    D4-S1: 内部hitは無条件採用ではなくreviewerの精査を経て根拠になる
    (統合index空間で外部候補が空のため、内部候補はindex 0から始まる)。
    """
    timeline: list[str] = []
    answerer = _EvidenceAnswerer(timeline=timeline)
    query_runtime = ScriptedAgentRuntime([_query_draft()])
    reviewer_runtime = ScriptedAgentRuntime([_review_draft_selecting([0])])
    tool = _Tool(
        errors={
            "NVIDIA": ExternalSearchProviderError(reason="tavily_search_http_error")
        }
    )
    factory = _Factory(
        [_runtime(query_runtime, reviewer_runtime=reviewer_runtime, tool=tool)],
        timeline,
    )
    phases = AnsweringPhases(
        planner=_Planner(_plan_with_tasks(("goal0", ["NVIDIA"]))),
        collector=NewsCollector(
            researcher=Researcher(
                internal_search=_InternalSearch(
                    hits=[_hit(assessment_id=1001, title="kept-hit")]
                )
            )
        ),
        external_runtime_factory=factory,
        direct_answerer=_UnreachableDirectAnswerer(),
        evidence_answerer=answerer,
        reviewer=EvidenceReviewer(),
    )
    runner = AnsweringRunner(
        input_safety_checker=AllowInputSafetyChecker(),
        context_preparer=_Preparer(),
        phases_factory=lambda: phases,
    )

    await _run(runner)

    assert [item.source.title for item in answerer.calls[0]] == ["kept-hit"]


@pytest.mark.asyncio
async def test_time_filter_failure_still_collects_internal_hits_for_every_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """保証するテスト条件 3(runner経由、全task)。

    D4-S1: reviewerのLLM runtimeを使えるようscopeは常にactivateされる
    (外部query/HTTP検索だけがtime filter失敗でskipされる)。内部候補は
    reviewerの精査を経て根拠になる。
    """
    captured = _capture_external_outcome(monkeypatch)
    internal = _KeyedFailingInternalSearch(
        failing_queries=set(),
        hits_by_query={
            "task0 query": [_hit(assessment_id=1001, title="task0-hit")],
            "task1 query": [_hit(assessment_id=1002, title="task1-hit")],
        },
    )
    plan = _plan_with_tasks(
        ("goal0", ["task0 query"]),
        ("goal1", ["task1 query"]),
        target_time_window=TargetTimeWindow(kind="unsupported_explicit_window"),
    )
    # S1: reviewerはRun単位1回。統合index空間(仮定: task昇順)ではtask0の唯一の
    # 内部候補が0、task1の唯一の内部候補が1。
    reviewer_runtime = ScriptedAgentRuntime([_review_draft_selecting([0, 1])])
    factory = _Factory(
        [_runtime(ScriptedAgentRuntime([]), reviewer_runtime=reviewer_runtime)], []
    )
    runner = _runner(plan=plan, internal=internal, factory=factory, timeline=[])

    await _run(runner)

    reports = _task_reports(captured[0])
    evidence_run = _completed_evidence_run(captured[0])
    assert (
        factory.activate_calls,
        sorted(report.external_collection for report in reports),
        # time filter失敗でも内部精査の完了結果はCompletedになる。
        isinstance(evidence_run, EvidenceRunCompleted),
        {item.title for item in evidence_run.answer_evidence.internal_articles},
    ) == (
        1,
        ["time_filter_failed", "time_filter_failed"],
        True,
        {"task0-hit", "task1-hit"},
    )


@pytest.mark.asyncio
async def test_runner_routes_each_tasks_queries_to_only_that_tasks_search() -> None:
    """保証するテスト条件 4(runner経由)。

    D4-S1: scopeは常にactivateされるが、内部・外部候補とも空
    (internalはhits無し、externalはtime filter失敗でskip)のためreviewerは
    起動しない。
    """
    internal = _InternalSearch()
    plan = _plan_with_tasks(
        ("goal0", ["q-a", "q-b"]),
        ("goal1", ["q-c"]),
        target_time_window=TargetTimeWindow(kind="unsupported_explicit_window"),
    )
    runner = _runner(
        plan=plan,
        internal=internal,
        factory=_Factory([_runtime(ScriptedAgentRuntime([]))], []),
        timeline=[],
    )

    await _run(runner)

    assert sorted(call.queries for call in internal.calls) == [
        ("q-a", "q-b"),
        ("q-c",),
    ]


@pytest.mark.asyncio
async def test_runner_isolates_one_tasks_total_failure_from_sibling_evidence() -> None:
    """保証するテスト条件 5(runner経由)。

    D4-S1: failing taskは内部・外部とも候補ゼロでreviewer未起動のまま
    time_filter_failedとして閉じる。succeeding taskは内部候補がreviewerの
    精査を経て根拠になる。
    """
    timeline: list[str] = []
    answerer = _EvidenceAnswerer(timeline=timeline)
    internal = _KeyedFailingInternalSearch(
        failing_queries={"failing query"},
        hits_by_query={
            "succeeding query": [_hit(assessment_id=1001, title="sibling-hit")]
        },
    )
    plan = _plan_with_tasks(
        ("failing goal", ["failing query"]),
        ("succeeding goal", ["succeeding query"]),
        target_time_window=TargetTimeWindow(kind="unsupported_explicit_window"),
    )
    reviewer_runtime = ScriptedAgentRuntime([_review_draft_selecting([0])])
    phases = AnsweringPhases(
        planner=_Planner(plan),
        collector=NewsCollector(researcher=Researcher(internal_search=internal)),
        external_runtime_factory=_Factory(
            [_runtime(ScriptedAgentRuntime([]), reviewer_runtime=reviewer_runtime)], []
        ),
        direct_answerer=_UnreachableDirectAnswerer(),
        evidence_answerer=answerer,
        reviewer=EvidenceReviewer(),
    )
    runner = AnsweringRunner(
        input_safety_checker=AllowInputSafetyChecker(),
        context_preparer=_Preparer(),
        phases_factory=lambda: phases,
    )

    await _run(runner)

    assert [item.source.title for item in answerer.calls[0]] == ["sibling-hit"]


@pytest.mark.asyncio
async def test_internal_hits_are_kept_per_task_when_the_same_article_appears(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """同じ内部検索の記事でもtaskが異なる場合は両方残る。

    D4-S1: 各taskが提示する2件の内部候補をreviewerが両方採用する前提で、
    task内の重複排除とtask間の共存を検証する。
    """
    captured = _capture_external_outcome(monkeypatch)
    shared_curation_id = 4242
    completion_order: list[str] = []

    class _RaceControlledInternalSearch:
        @property
        def name(self) -> str:
            return "internal_search"

        async def search(self, input: Any) -> list[InternalArticleSearchHit]:
            query = input.queries.queries[0]
            if query == "task0 query":
                await asyncio.sleep(0.05)
                completion_order.append("task0")
                return [
                    _hit(
                        assessment_id=1000,
                        title="task0-shared",
                        curation_id=shared_curation_id,
                        distance=0.9,
                    ),
                    _hit(assessment_id=1001, title="task0-unique", curation_id=100),
                ]
            completion_order.append("task1")
            return [
                _hit(
                    assessment_id=2000,
                    title="task1-shared",
                    curation_id=shared_curation_id,
                    distance=0.1,
                ),
                _hit(assessment_id=2001, title="task1-unique", curation_id=200),
            ]

    plan = _plan_with_tasks(
        ("goal0", ["task0 query"]),
        ("goal1", ["task1 query"]),
        target_time_window=TargetTimeWindow(kind="unsupported_explicit_window"),
    )
    # S1: reviewerはRun単位1回。統合index空間(仮定: task昇順)ではtask0の2件が
    # 0,1(task0-shared, task0-unique)、task1の2件が2,3(task1-shared, task1-unique)。
    # task単位で呼ぶ旧経路が残っていても2件目のcallがscript枯渇crashにならない
    # よう空draftを1件足す。
    reviewer_runtime = ScriptedAgentRuntime(
        [_review_draft_selecting([0, 1, 2, 3]), _review_draft_selecting([])]
    )
    runner = _runner(
        plan=plan,
        internal=_RaceControlledInternalSearch(),
        factory=_Factory(
            [_runtime(ScriptedAgentRuntime([]), reviewer_runtime=reviewer_runtime)], []
        ),
        timeline=[],
    )

    await asyncio.wait_for(_run(runner), timeout=1.0)

    assert (
        completion_order,
        [
            item.title
            for item in _completed_evidence_run(
                captured[0]
            ).answer_evidence.internal_articles
        ],
    ) == (
        ["task1", "task0"],
        ["task0-shared", "task0-unique", "task1-shared", "task1-unique"],
    )


@pytest.mark.asyncio
async def test_all_tasks_incomplete_adds_the_fixed_incomplete_phrase_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """保証するテスト条件 2・4。run単位のcollection_failuresは廃止され、

    task reportのinternal_collection="failed"・review="skipped_empty"へ
    一本化される。経路名文言ではなく固定文言「完了できなかった調査があります」
    が出る(taskが複数落ちても1行)。
    """
    captured = _capture_external_outcome(monkeypatch)
    internal = _KeyedFailingInternalSearch(
        failing_queries={"task0 query", "task1 query"},
    )
    plan = _plan_with_tasks(
        ("goal0", ["task0 query"]),
        ("goal1", ["task1 query"]),
        target_time_window=TargetTimeWindow(kind="unsupported_explicit_window"),
    )
    runner = _runner(
        plan=plan,
        internal=internal,
        factory=_Factory([_runtime(ScriptedAgentRuntime([]))], []),
        timeline=[],
    )

    result = await runner.run(
        RunInput(question="NVIDIA の見通しは？", history=()),
        run_context=RUN_CONTEXT,
    )

    assert result.final_output.status == "insufficient"
    assert (
        "内部記事検索を完了できませんでした" not in result.final_output.missing_aspects
    )
    assert (
        result.final_output.missing_aspects.count("完了できなかった調査があります") == 1
    )
    assert {report.internal_collection for report in _task_reports(captured[0])} == {
        "failed"
    }
    assert _completed_evidence_run(captured[0]).answer_evidence.count == 0


@pytest.mark.asyncio
async def test_some_tasks_incomplete_keeps_the_phrase_to_one_line_and_keeps_sources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """保証するテスト条件 2・7。一部taskだけ未完了でも固定文言は1行、

    生き残ったtaskの内部根拠(reviewer精査済み)は維持される。
    """
    captured = _capture_external_outcome(monkeypatch)
    internal = _KeyedFailingInternalSearch(
        failing_queries={"task0 query"},
        hits_by_query={"task1 query": [_hit(assessment_id=1001, title="survivor")]},
    )
    plan = _plan_with_tasks(
        ("goal0", ["task0 query"]),
        ("goal1", ["task1 query"]),
        target_time_window=TargetTimeWindow(kind="unsupported_explicit_window"),
    )
    reviewer_runtime = ScriptedAgentRuntime([_review_draft_selecting([0])])
    runner = _runner(
        plan=plan,
        internal=internal,
        factory=_Factory(
            [_runtime(ScriptedAgentRuntime([]), reviewer_runtime=reviewer_runtime)], []
        ),
        timeline=[],
    )

    await _run(runner)

    reports = {report.task_index: report for report in _task_reports(captured[0])}
    evidence_run = _completed_evidence_run(captured[0])
    assert reports[0].internal_collection == "failed"
    assert reports[1].internal_collection == "succeeded"
    assert [item.title for item in evidence_run.answer_evidence.internal_articles] == [
        "survivor"
    ]
