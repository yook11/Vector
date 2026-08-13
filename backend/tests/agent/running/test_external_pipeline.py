"""AnsweringRunner が所有する external Query -> Tool -> Evidence Reviewer 契約。"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from contextlib import AbstractAsyncContextManager
from datetime import UTC, date, datetime
from typing import Any
from uuid import UUID

import pytest
from logfire.testing import CaptureLogfire
from structlog.testing import capture_logs

import app.agent.planning.contract as planning_contract
from app.agent.answering.contract import AnsweringRequest
from app.agent.answering.direct_answer.contract import DirectAnswerDraft
from app.agent.answering.evidence_answer.contract import (
    EvidenceAnswerDraft,
    EvidenceAnswerOutcome,
    EvidenceAnswerUnavailable,
)
from app.agent.evidence_collection import NewsCollector, Researcher
from app.agent.evidence_collection import (
    news_collector as news_collector_module,
)
from app.agent.evidence_collection.external_search import (
    ExternalResearchRuntime,
    ExternalSearchCandidate,
    ExternalSearchDateFilter,
    ExternalSearchProviderError,
)
from app.agent.evidence_collection.external_search.contract import (
    EXTERNAL_SEARCH_CANDIDATES_PER_QUERY,
)
from app.agent.evidence_collection.internal_search import (
    InternalArticleContent,
    InternalArticleSearchHit,
)
from app.agent.evidence_review import EvidenceReviewer
from app.agent.planning.contract import (
    ExternalResearchTask,
    PlanningRequest,
    TargetTimeWindow,
)
from app.agent.question_context import QuestionContext
from app.agent.running import AnsweringPhases, AnsweringRunner, RunContext, RunInput
from app.agent.running import answering_runner as answering_runner_module
from app.agent.runtime.contract import AgentResponseDefect, AgentResponseInvalidError
from app.analysis.ai_provider_errors import AIProviderNetworkError
from app.analysis.analyzed_article import InScopeAnalyzedArticle
from app.analysis.assessment.domain.result import InScope, InScopeCategory
from app.analysis.deepseek_error_translator import DeepSeekStateReason
from tests.agent.running._input_safety import AllowInputSafetyChecker
from tests.agent.runtime._fakes import ScriptedAgentRuntime
from tests.logfire._metric_helpers import collected_metrics

RUN_ID = UUID("019bd239-1ed4-7fbb-a336-04fe3c197652")
AS_OF = datetime(2026, 7, 20, 9, 30, tzinfo=UTC)
_DEFAULT_TARGET_TIME_WINDOW = TargetTimeWindow(kind="last_n_days", days=1)
_TIME_FILTER_METRIC = "external_search_time_filter_resolution_total"


def _task(goal: str) -> ExternalResearchTask:
    return ExternalResearchTask(research_goal=goal)


def _plan(
    tasks: list[ExternalResearchTask],
    *,
    target_time_window: TargetTimeWindow | None = _DEFAULT_TARGET_TIME_WINDOW,
) -> Any:
    plan_type = getattr(planning_contract, "SearchPlan", None)
    research_task_type = getattr(planning_contract, "ResearchTask", None)
    if plan_type is None or research_task_type is None:
        pytest.fail("planning contract must define SearchPlan and ResearchTask")
    return plan_type(
        research_tasks=[
            research_task_type(
                research_goal=task.research_goal,
                article_search_queries=["NVIDIA"],
            )
            for task in tasks
        ],
        target_time_window=target_time_window,
    )


def _query_draft(queries: object) -> Any:
    from app.agent.evidence_collection.external_search.contract import (
        ExternalQueryDraft,
    )

    return ExternalQueryDraft.model_validate({"queries": queries})


def _review_draft(
    selections: list[dict[str, Any]] | None = None,
    *,
    missing: list[str] | None = None,
) -> Any:
    from app.agent.evidence_review import EvidenceReviewDraft

    return EvidenceReviewDraft.model_validate(
        {"selections": selections or [], "missing": missing or []}
    )


def _candidate(url: str, *, title: str | None = None) -> ExternalSearchCandidate:
    return ExternalSearchCandidate(
        url=url,
        title=title or url.rsplit("/", maxsplit=1)[-1],
        snippet="snippet",
        source_name="Example",
        published_at=AS_OF,
    )


class _Preparer:
    async def prepare(self, **_kwargs: object) -> QuestionContext:
        return QuestionContext(standalone_question="NVIDIA の見通しは？")


class _Planner:
    def __init__(self, plan: Any) -> None:
        self.plan_result = plan
        self.calls: list[PlanningRequest] = []

    async def plan(self, request: PlanningRequest) -> Any:
        self.calls.append(request)
        return self.plan_result


class _EmptyInternalSearch:
    @property
    def name(self) -> str:
        return "internal_search"

    async def invoke(self, input: object) -> list[object]:
        del input
        return []


def _internal_hit(*, assessment_id: int, title: str) -> InternalArticleSearchHit:
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


class _OneInternalHitSearch:
    """internal+externalの合算件数を確かめるため、内部hitを1件返す。"""

    @property
    def name(self) -> str:
        return "internal_search"

    async def invoke(self, input: object) -> list[InternalArticleSearchHit]:
        del input
        return [_internal_hit(assessment_id=2001, title="internal hit")]


class _UnreachableDirectAnswerer:
    async def answer(
        self, *, request: AnsweringRequest, previous_answer: str = ""
    ) -> DirectAnswerDraft:
        raise AssertionError(
            f"direct answer must not run: {request!r} {previous_answer!r}"
        )


class _EvidenceAnswerer:
    def __init__(self) -> None:
        self.calls: list[list[Any]] = []

    async def answer(
        self,
        *,
        request: AnsweringRequest,
        evidence: list[Any],
        target_time_window: TargetTimeWindow | None,
        review_missing: tuple[str, ...] = (),
    ) -> EvidenceAnswerOutcome:
        del request, target_time_window, review_missing
        self.calls.append(list(evidence))
        if evidence:
            return EvidenceAnswerDraft(
                answer="根拠に基づく回答です。",
                cited_refs=[item.source.source_ref for item in evidence],
            )
        # 自己申告でinsufficientを名乗る形は無くなったため、
        # evidenceが無く回答を作れなかった場合はunavailableで表す。
        return EvidenceAnswerUnavailable(failure_code="fake_no_evidence")


class _Scope(AbstractAsyncContextManager[ExternalResearchRuntime]):
    def __init__(self, runtime: ExternalResearchRuntime, timeline: list[str]) -> None:
        self._runtime = runtime
        self._timeline = timeline
        self.entered = False
        self.exited = False
        self.exit_calls = 0

    async def __aenter__(self) -> ExternalResearchRuntime:
        self.entered = True
        self._timeline.append("scope.enter")
        return self._runtime

    async def __aexit__(
        self,
        exc_type: object,
        exc: BaseException | None,
        traceback: object,
    ) -> bool:
        del exc_type, exc, traceback
        self.exit_calls += 1
        self.exited = True
        self._timeline.append("scope.exit")
        return False


class _Factory:
    def __init__(
        self,
        runtimes: Sequence[ExternalResearchRuntime],
        *,
        timeline: list[str] | None = None,
    ) -> None:
        self._runtimes = list(runtimes)
        self.timeline = timeline if timeline is not None else []
        self.scopes: list[_Scope] = []

    def activate(self) -> _Scope:
        runtime = self._runtimes.pop(0)
        scope = _Scope(runtime, self.timeline)
        self.scopes.append(scope)
        return scope


class _Tool:
    def __init__(
        self,
        results_by_query: dict[str, list[ExternalSearchCandidate]] | None = None,
        *,
        errors_by_query: dict[str, BaseException] | None = None,
        started: asyncio.Event | None = None,
        release: asyncio.Event | None = None,
    ) -> None:
        self._results_by_query = results_by_query or {}
        self._errors_by_query = errors_by_query or {}
        self._started = started
        self._release = release
        self.calls: list[Any] = []
        self.cancelled = False

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
            if input.query in self._errors_by_query:
                raise self._errors_by_query[input.query]
            return list(self._results_by_query.get(input.query, []))
        except asyncio.CancelledError:
            self.cancelled = True
            raise


class _Events:
    def __init__(self) -> None:
        self.events: list[Any] = []

    async def event_occurred(self, event: Any) -> None:
        self.events.append(event)


def _external_search_events(events: list[Any]) -> list[Any]:
    """同じreporterへ並走発火するinternal_search系eventを除き、外部検索系+選別eventを残す。"""

    return [
        event
        for event in events
        if not event.type.startswith("evidence_collection.internal_search_")
    ]


class _ParallelQueryRuntime:
    def __init__(self, *, release: asyncio.Event) -> None:
        self._release = release
        self.two_started = asyncio.Event()
        self.active = 0
        self.peak = 0

    async def invoke(self, agent: object, input: Any, *, attempt_number: int) -> Any:
        del agent, attempt_number
        self.active += 1
        self.peak = max(self.peak, self.active)
        if self.active >= 2:
            self.two_started.set()
        try:
            await self._release.wait()
            return _query_draft([input.task.research_goal])
        finally:
            self.active -= 1


class _NeverCompletingRuntime:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.cancelled = False
        self.calls: list[Any] = []
        self.attempt_numbers: list[int] = []

    async def invoke(self, agent: object, input: Any, *, attempt_number: int) -> Any:
        del agent
        self.calls.append(input)
        self.attempt_numbers.append(attempt_number)
        self.started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise


class _AllTasksBlockingRuntime:
    def __init__(self, *, task_count: int, timeline: list[str]) -> None:
        self._task_count = task_count
        self._timeline = timeline
        self.all_tasks_started = asyncio.Event()
        self.all_tasks_finished = asyncio.Event()
        self.started_count = 0
        self.cancelled_count = 0
        self.finished_count = 0

    async def invoke(self, agent: object, input: object, *, attempt_number: int) -> Any:
        del agent, input, attempt_number
        self.started_count += 1
        if self.started_count == self._task_count:
            self.all_tasks_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled_count += 1
            raise
        finally:
            self.finished_count += 1
            self._timeline.append("external_task.finished")
            if self.finished_count == self._task_count:
                self.all_tasks_finished.set()


class _TaskFailureAfterSiblingStartsRuntime:
    def __init__(self, *, error: BaseException, timeline: list[str]) -> None:
        self._error = error
        self._timeline = timeline
        self.sibling_started = asyncio.Event()
        self.sibling_finished = asyncio.Event()
        self.sibling_cancelled = False

    async def invoke(self, agent: object, input: Any, *, attempt_number: int) -> Any:
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


class _QueryFailureAfterSiblingStartsTool(_Tool):
    def __init__(self, *, error: BaseException) -> None:
        super().__init__()
        self._error = error
        self.sibling_started = asyncio.Event()
        self.sibling_finished = asyncio.Event()
        self.sibling_cancelled = False

    async def invoke(self, input: Any) -> list[ExternalSearchCandidate]:
        self.calls.append(input)
        if input.query == "failing":
            await self.sibling_started.wait()
            raise self._error
        self.sibling_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.sibling_cancelled = True
            raise
        finally:
            self.sibling_finished.set()


def _runtime(
    *,
    query_runtime: object,
    reviewer_runtime: object,
    tool: object,
) -> ExternalResearchRuntime:
    return ExternalResearchRuntime(
        query_runtime=query_runtime,  # type: ignore[arg-type]
        reviewer_runtime=reviewer_runtime,  # type: ignore[arg-type]
        search_tool=tool,  # type: ignore[arg-type]
    )


def _runner(
    *,
    tasks: list[ExternalResearchTask],
    runtime: ExternalResearchRuntime,
    events: _Events | None = None,
    requested_agent_count: int | None = None,
    timeline: list[str] | None = None,
    target_time_window: TargetTimeWindow | None = _DEFAULT_TARGET_TIME_WINDOW,
) -> tuple[AnsweringRunner, _EvidenceAnswerer, _Factory]:
    answerer = _EvidenceAnswerer()
    factory = _Factory([runtime], timeline=timeline)
    phases = AnsweringPhases(
        planner=_Planner(
            _plan(tasks, target_time_window=target_time_window),
        ),
        collector=NewsCollector(
            researcher=Researcher(
                internal_search=_EmptyInternalSearch(), events=events
            ),
            requested_agent_count=requested_agent_count,
        ),
        external_runtime_factory=factory,
        direct_answerer=_UnreachableDirectAnswerer(),
        evidence_answerer=answerer,
        reviewer=EvidenceReviewer(),
    )
    return (
        AnsweringRunner(
            input_safety_checker=AllowInputSafetyChecker(),
            context_preparer=_Preparer(),
            phases_factory=lambda: phases,
            events=events,
        ),
        answerer,
        factory,
    )


async def _run(runner: AnsweringRunner, *, as_of: datetime = AS_OF) -> Any:
    return await runner.run(
        RunInput(question="NVIDIA の見通しは？", history=()),
        run_context=RunContext(run_id=RUN_ID, as_of=as_of),
    )


def _capture_external_outcome(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    captured: list[Any] = []
    original = answering_runner_module.normalize_answer_evidence

    def capture(outcome: Any) -> Any:
        captured.append(outcome)
        return original(outcome)

    monkeypatch.setattr(answering_runner_module, "normalize_answer_evidence", capture)
    return captured


def _time_filter_metric_points(
    metrics: list[dict[str, Any]],
) -> list[tuple[int, dict[str, Any]]]:
    metric = next(
        (item for item in metrics if item["name"] == _TIME_FILTER_METRIC),
        None,
    )
    if metric is None:
        return []
    return [
        (int(point["value"]), point.get("attributes", {}))
        for point in metric["data"]["data_points"]
    ]


def _record_and_shorten_pipeline_timeouts(
    monkeypatch: pytest.MonkeyPatch,
) -> list[float]:
    original_wait_for = asyncio.wait_for
    observed: list[float] = []

    async def wait_for(awaitable: Any, timeout: float) -> Any:
        observed.append(timeout)
        bounded_timeout = 0.001 if timeout in {15, 30} else timeout
        return await original_wait_for(awaitable, timeout=bounded_timeout)

    monkeypatch.setattr(asyncio, "wait_for", wait_for)
    return observed


@pytest.mark.asyncio
async def test_external_pipeline_normalizes_queries_and_hides_urls_from_reviewer() -> (
    None
):
    long_query = "x" * 205
    query_runtime = ScriptedAgentRuntime(
        [_query_draft(["  normalized  ", "normalized", long_query, "third", "fourth"])]
    )
    reviewer_runtime = ScriptedAgentRuntime(
        [
            _review_draft(
                [{"candidate_index": 1, "claim": "claim", "why_selected": "why"}]
            )
        ]
    )
    tool = _Tool(
        {
            "normalized": [_candidate("https://example.com/first")],
            "x" * 200: [_candidate("https://example.com/second")],
            "third": [_candidate("https://example.com/third")],
        }
    )
    runner, answerer, _ = _runner(
        tasks=[_task("collect evidence")],
        runtime=_runtime(
            query_runtime=query_runtime,
            reviewer_runtime=reviewer_runtime,
            tool=tool,
        ),
    )

    result = await _run(runner)
    reviewer_input = reviewer_runtime.calls[0].input

    assert [call.query for call in tool.calls] == [
        "normalized",
        "x" * 200,
        "third",
    ]
    assert all(call.limit == 10 for call in tool.calls)
    assert all(
        not hasattr(candidate, "url")
        for candidate in reviewer_input.task_groups[0].candidates
    )
    assert (
        [(item.source.title, item.source.evidence_claim) for item in answerer.calls[0]],
        result.final_output.status,
    ) == ([("second", "claim")], "answered")


@pytest.mark.asyncio
async def test_external_pipeline_passes_resolved_filter_to_every_tool_call() -> None:
    target_time_window = TargetTimeWindow(kind="last_n_days", days=7)
    query_runtime = ScriptedAgentRuntime(
        [_query_draft(["first"]), _query_draft(["second"])]
    )
    tool = _Tool(
        {
            "first": [_candidate("https://example.com/first")],
            "second": [_candidate("https://example.com/second")],
        }
    )
    runner, _, _ = _runner(
        tasks=[_task("first task"), _task("second task")],
        runtime=_runtime(
            query_runtime=query_runtime,
            reviewer_runtime=ScriptedAgentRuntime(
                [_review_draft([]), _review_draft([])]
            ),
            tool=tool,
        ),
        target_time_window=target_time_window,
    )

    await _run(runner)

    assert (
        [call.date_filter for call in tool.calls],
        [call.input.target_time_window for call in query_runtime.calls],
    ) == (
        [
            ExternalSearchDateFilter(
                start_date=datetime(2026, 7, 13, tzinfo=UTC).date(),
                end_date=datetime(2026, 7, 21, tzinfo=UTC).date(),
            ),
            ExternalSearchDateFilter(
                start_date=datetime(2026, 7, 13, tzinfo=UTC).date(),
                end_date=datetime(2026, 7, 21, tzinfo=UTC).date(),
            ),
        ],
        [target_time_window, target_time_window],
    )


@pytest.mark.asyncio
async def test_external_pipeline_passes_explicit_none_filter_to_tool() -> None:
    tool = _Tool({"query": [_candidate("https://example.com/no-filter")]})
    runner, _, _ = _runner(
        tasks=[_task("no publication filter")],
        runtime=_runtime(
            query_runtime=ScriptedAgentRuntime([_query_draft(["query"])]),
            reviewer_runtime=ScriptedAgentRuntime([_review_draft([])]),
            tool=tool,
        ),
        target_time_window=None,
    )

    await _run(runner)

    assert [call.date_filter for call in tool.calls] == [None]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("target_time_window", "expected_tool_call_count"),
    [
        pytest.param(None, 4, id="not-requested"),
        pytest.param(
            TargetTimeWindow(kind="last_n_days", days=1),
            4,
            id="resolved",
        ),
        pytest.param(
            TargetTimeWindow(kind="unsupported_explicit_window"),
            0,
            id="resolution-failed",
        ),
    ],
)
async def test_external_runner_resolves_target_time_window_once_per_branch(
    monkeypatch: pytest.MonkeyPatch,
    target_time_window: TargetTimeWindow | None,
    expected_tool_call_count: int,
) -> None:
    original_resolver = news_collector_module.resolve_external_search_date_filter
    resolver_calls: list[tuple[TargetTimeWindow | None, datetime]] = []

    def spy(
        target: TargetTimeWindow | None,
        *,
        as_of: datetime,
    ) -> ExternalSearchDateFilter | None:
        resolver_calls.append((target, as_of))
        return original_resolver(target, as_of=as_of)

    monkeypatch.setattr(
        news_collector_module,
        "resolve_external_search_date_filter",
        spy,
    )
    tasks = [_task("first period task"), _task("second period task")]
    tool = _Tool()
    runner, _, _ = _runner(
        tasks=tasks,
        runtime=_runtime(
            query_runtime=ScriptedAgentRuntime(
                [
                    _query_draft(["first-1", "first-2"]),
                    _query_draft(["second-1", "second-2"]),
                ]
            ),
            reviewer_runtime=ScriptedAgentRuntime([]),
            tool=tool,
        ),
        target_time_window=target_time_window,
    )

    await _run(runner)

    assert (
        resolver_calls,
        len(tasks),
        len(tool.calls),
    ) == (
        [(target_time_window, AS_OF)],
        2,
        expected_tool_call_count,
    )


@pytest.mark.asyncio
async def test_naive_as_of_propagates_before_external_activity_or_observability(
    capfire: CaptureLogfire,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_resolver = news_collector_module.resolve_external_search_date_filter
    resolver_calls: list[tuple[TargetTimeWindow | None, datetime]] = []
    naive_as_of = datetime(2026, 7, 20, 9, 30)

    def spy(
        target: TargetTimeWindow | None,
        *,
        as_of: datetime,
    ) -> ExternalSearchDateFilter | None:
        resolver_calls.append((target, as_of))
        return original_resolver(target, as_of=as_of)

    monkeypatch.setattr(
        news_collector_module,
        "resolve_external_search_date_filter",
        spy,
    )
    captured = _capture_external_outcome(monkeypatch)
    events = _Events()
    query_runtime = ScriptedAgentRuntime([])
    reviewer_runtime = ScriptedAgentRuntime([])
    tool = _Tool()
    runner, answerer, factory = _runner(
        tasks=[_task("naive as_of は分類しない")],
        runtime=_runtime(
            query_runtime=query_runtime,
            reviewer_runtime=reviewer_runtime,
            tool=tool,
        ),
        events=events,
    )

    with capture_logs() as logs, pytest.raises(ValueError):
        await _run(runner, as_of=naive_as_of)
    metrics = collected_metrics(capfire)

    assert (
        resolver_calls,
        factory.scopes,
        query_runtime.calls,
        reviewer_runtime.calls,
        tool.calls,
        _external_search_events(events.events),
        answerer.calls,
        captured,
        _time_filter_metric_points(metrics),
        [
            entry
            for entry in logs
            if entry.get("event") == "external_search_time_filter_failed"
        ],
    ) == (
        [(_DEFAULT_TARGET_TIME_WINDOW, naive_as_of)],
        [],
        [],
        [],
        [],
        [],
        [],
        [],
        [],
        [],
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("target_time_window", "expected_result"),
    [
        pytest.param(None, "not_requested", id="not-requested"),
        pytest.param(
            TargetTimeWindow(kind="last_n_days", days=1),
            "resolved",
            id="resolved",
        ),
    ],
)
async def test_external_branch_records_one_nonfailed_time_filter_resolution_metric(
    capfire: CaptureLogfire,
    target_time_window: TargetTimeWindow | None,
    expected_result: str,
) -> None:
    tool = _Tool()
    runner, _, factory = _runner(
        tasks=[_task("期間計測")],
        runtime=_runtime(
            query_runtime=ScriptedAgentRuntime([_query_draft(["metric query"])]),
            reviewer_runtime=ScriptedAgentRuntime([]),
            tool=tool,
        ),
        target_time_window=target_time_window,
    )

    with capture_logs() as logs:
        await _run(runner)
    metrics = collected_metrics(capfire)

    assert (
        _time_filter_metric_points(metrics),
        [
            entry
            for entry in logs
            if entry.get("event") == "external_search_time_filter_failed"
        ],
        len(factory.scopes),
    ) == (
        [(1, {"result": expected_result, "reason": "none"})],
        [],
        1,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("target_time_window", "expected_reason"),
    [
        pytest.param(
            TargetTimeWindow(kind="unsupported_explicit_window"),
            "unsupported_explicit_window",
            id="unsupported-explicit-window",
        ),
        pytest.param(
            TargetTimeWindow(kind="calendar_month", year=2026, month=8),
            "future_calendar_month",
            id="future-calendar-month",
        ),
        pytest.param(
            TargetTimeWindow(
                kind="date_range",
                start_date=date(2026, 7, 21),
                end_date_inclusive=date(2026, 7, 21),
            ),
            "future_date_range",
            id="future-date-range",
        ),
        pytest.param(
            TargetTimeWindow(
                kind="date_range",
                start_date=date.min,
                end_date_inclusive=date.min,
            ),
            "unexpandable_start_date",
            id="unexpandable-start-date",
        ),
    ],
)
async def test_time_filter_resolution_failure_closes_external_branch_before_activity(
    capfire: CaptureLogfire,
    monkeypatch: pytest.MonkeyPatch,
    target_time_window: TargetTimeWindow,
    expected_reason: str,
) -> None:
    captured = _capture_external_outcome(monkeypatch)
    events = _Events()
    query_runtime = ScriptedAgentRuntime([])
    reviewer_runtime = ScriptedAgentRuntime([])
    tool = _Tool()
    tasks = [_task("first closed task"), _task("second closed task")]
    runner, answerer, factory = _runner(
        tasks=tasks,
        runtime=_runtime(
            query_runtime=query_runtime,
            reviewer_runtime=reviewer_runtime,
            tool=tool,
        ),
        events=events,
        target_time_window=target_time_window,
    )

    with capture_logs() as logs:
        await _run(runner)
    metrics = collected_metrics(capfire)
    reports = captured[0].task_reports

    assert (
        # D4-S1: reviewerがLLM runtimeを必要とするため、time filter失敗でも
        # external runtime scopeは常にactivateされる(外部query/HTTP検索だけを
        # skipする)。ただしinternal候補も空(_EmptyInternalSearch)のため両方
        # 候補ゼロとなりreviewer自体は呼ばれない(D4-S2: review=skipped_empty)。
        len(factory.scopes),
        factory.scopes[0].entered,
        factory.scopes[0].exit_calls,
        query_runtime.calls,
        reviewer_runtime.calls,
        tool.calls,
        _external_search_events(events.events),
        answerer.calls,
        [
            (
                report.task_index,
                report.internal_collection,
                report.external_collection,
                report.time_filter_failure_reason,
                report.generated_queries,
                report.provider_failed_query_count,
                report.internal_candidate_count,
                report.external_candidate_count,
            )
            for report in reports
        ],
        # S1: reviewはtask単位のfieldではなくEvidenceCollectionOutcome.reviewへ
        # 移動した(両taskとも候補ゼロのためRun全体がskipped_emptyで1本になる)。
        (
            captured[0].review.review,
            captured[0].review.internal_evidence_count,
            captured[0].review.external_evidence_count,
            captured[0].review.dropped_selection_count,
            captured[0].review.review_failure_reason,
            captured[0].review.missing,
        ),
        _time_filter_metric_points(metrics),
        [
            entry
            for entry in logs
            if entry.get("event") == "external_search_time_filter_failed"
        ],
    ) == (
        1,
        True,
        1,
        [],
        [],
        [],
        [],
        [[]],
        [
            (
                0,
                "succeeded",
                "time_filter_failed",
                expected_reason,
                [],
                0,
                0,
                0,
            ),
            (
                1,
                "succeeded",
                "time_filter_failed",
                expected_reason,
                [],
                0,
                0,
                0,
            ),
        ],
        ("skipped_empty", 0, 0, 0, None, []),
        [(1, {"result": "failed", "reason": expected_reason})],
        [
            {
                "reason": expected_reason,
                "task_count": 2,
                "event": "external_search_time_filter_failed",
                "log_level": "warning",
            }
        ],
    )


@pytest.mark.asyncio
async def test_provider_result_cap_is_applied_before_candidate_pool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = _capture_external_outcome(monkeypatch)
    reviewer_runtime = ScriptedAgentRuntime([_review_draft([])])
    runner, _, _ = _runner(
        tasks=[_task("provider result cap")],
        runtime=_runtime(
            query_runtime=ScriptedAgentRuntime([_query_draft(["q"])]),
            reviewer_runtime=reviewer_runtime,
            tool=_Tool(
                {
                    "q": [
                        _candidate(f"https://example.com/candidate-{index}")
                        for index in range(EXTERNAL_SEARCH_CANDIDATES_PER_QUERY + 3)
                    ]
                }
            ),
        ),
    )

    await _run(runner)

    candidates = reviewer_runtime.calls[0].input.task_groups[0].candidates
    assert (
        len(candidates),
        candidates[-1].title,
        captured[0].task_reports[0].external_candidate_count,
    ) == (
        EXTERNAL_SEARCH_CANDIDATES_PER_QUERY,
        f"candidate-{EXTERNAL_SEARCH_CANDIDATES_PER_QUERY - 1}",
        EXTERNAL_SEARCH_CANDIDATES_PER_QUERY,
    )


@pytest.mark.asyncio
async def test_classified_query_failure_never_starts_tool_or_reviewer() -> None:
    query_runtime = ScriptedAgentRuntime(
        [AgentResponseInvalidError(AgentResponseDefect.RESPONSE_NOT_JSON)]
    )
    reviewer_runtime = ScriptedAgentRuntime([])
    tool = _Tool()
    runner, answerer, factory = _runner(
        tasks=[_task("invalid query")],
        runtime=_runtime(
            query_runtime=query_runtime,
            reviewer_runtime=reviewer_runtime,
            tool=tool,
        ),
    )

    await _run(runner)

    assert (
        tool.calls,
        reviewer_runtime.calls,
        answerer.calls,
        factory.scopes[0].exit_calls,
    ) == ([], [], [[]], 1)


@pytest.mark.asyncio
async def test_partial_provider_failure_continues_but_all_failure_skips_reviewer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = _capture_external_outcome(monkeypatch)
    provider_error = ExternalSearchProviderError(reason="tavily_search_http_error")
    query_runtime = ScriptedAgentRuntime(
        [_query_draft(["good", "bad"]), _query_draft(["bad"])]
    )
    reviewer_runtime = ScriptedAgentRuntime([_review_draft([])])
    tool = _Tool(
        {"good": [_candidate("https://example.com/good")]},
        errors_by_query={"bad": provider_error},
    )
    runner, answerer, _ = _runner(
        tasks=[_task("partial failure"), _task("complete failure")],
        runtime=_runtime(
            query_runtime=query_runtime,
            reviewer_runtime=reviewer_runtime,
            tool=tool,
        ),
        requested_agent_count=1,
    )

    await _run(runner)

    assert (
        [call.query for call in tool.calls],
        len(reviewer_runtime.calls),
        answerer.calls,
        [
            (
                report.external_collection,
                report.provider_failed_query_count,
                report.external_candidate_count,
            )
            for report in captured[0].task_reports
        ],
        # S1: reviewはtask単位のfieldではなくEvidenceCollectionOutcome.reviewへ
        # 移動した。task0に候補が残るためRun全体としてreviewerが起動しsucceededになる。
        captured[0].review.review,
    ) == (
        ["good", "bad", "bad"],
        1,
        [[]],
        [
            ("succeeded", 1, 1),
            ("provider_failed", 1, 0),
        ],
        "succeeded",
    )


@pytest.mark.asyncio
async def test_empty_candidate_pool_skips_reviewer() -> None:
    reviewer_runtime = ScriptedAgentRuntime([])
    runner, answerer, _ = _runner(
        tasks=[_task("empty pool")],
        runtime=_runtime(
            query_runtime=ScriptedAgentRuntime([_query_draft(["q"])]),
            reviewer_runtime=reviewer_runtime,
            tool=_Tool({"q": []}),
        ),
    )

    result = await _run(runner)

    assert (reviewer_runtime.calls, answerer.calls, result.final_output.status) == (
        [],
        [[]],
        "insufficient",
    )


@pytest.mark.asyncio
async def test_reviewer_failure_after_two_attempts_becomes_failed_review_report(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """runnerがreviewer失敗をreview=failed reportへ写す結線を保証する。

    attempt/timeout/失敗分類の詳細な組み合わせは
    tests/agent/evidence_review/test_reviewer.py が正本。
    """
    captured = _capture_external_outcome(monkeypatch)
    failure = AgentResponseInvalidError(AgentResponseDefect.RESPONSE_NOT_JSON)
    reviewer_runtime = ScriptedAgentRuntime([failure, failure])
    runner, answerer, _ = _runner(
        tasks=[_task("reviewer failure")],
        runtime=_runtime(
            query_runtime=ScriptedAgentRuntime([_query_draft(["q"])]),
            reviewer_runtime=reviewer_runtime,
            tool=_Tool({"q": [_candidate("https://example.com/q")]}),
        ),
    )

    await _run(runner)

    report = captured[0].task_reports[0]
    review = captured[0].review
    assert (
        report.external_collection,
        review.review,
        review.review_failure_reason,
        review.internal_evidence_count,
        review.external_evidence_count,
        [call.attempt_number for call in reviewer_runtime.calls],
        answerer.calls,
    ) == ("succeeded", "failed", "response_not_json", 0, 0, [1, 2], [[]])


@pytest.mark.asyncio
async def test_workflow_constructs_task_ordered_external_outcome_before_answering(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = _capture_external_outcome(monkeypatch)
    tasks = [_task("first"), _task("second")]
    runner, _, _ = _runner(
        tasks=tasks,
        runtime=_runtime(
            query_runtime=ScriptedAgentRuntime(
                [_query_draft(["q1"]), _query_draft(["q2"])]
            ),
            # S1: reviewerはRun単位1回。統合index空間(仮定: task昇順)ではtask0の
            # 唯一の候補が0、task1の唯一の候補が1になる。task単位で呼ぶ旧経路が
            # 残っていても2件目のcallがscript枯渇crashにならないよう空draftを足す。
            reviewer_runtime=ScriptedAgentRuntime(
                [
                    _review_draft(
                        [
                            {
                                "candidate_index": 0,
                                "claim": "first claim",
                                "why_selected": "why",
                            },
                            {
                                "candidate_index": 1,
                                "claim": "second claim",
                                "why_selected": "why",
                            },
                        ]
                    ),
                    _review_draft([]),
                ]
            ),
            tool=_Tool(
                {
                    "q1": [_candidate("https://example.com/shared", title="first")],
                    "q2": [_candidate("https://example.com/shared", title="second")],
                }
            ),
        ),
        requested_agent_count=4,
    )

    await _run(runner)

    outcome = captured[0].external_search
    reports = captured[0].task_reports
    review = captured[0].review
    assert (
        [report.research_goal for report in reports],
        outcome.requested_agent_count,
        outcome.effective_agent_count,
        outcome.hard_agent_limit,
        [
            (
                report.task_index,
                report.external_collection,
                report.generated_queries,
                report.external_candidate_count,
            )
            for report in reports
        ],
        # S1(合流と重複排除): 外部根拠のURL重複排除は廃止されたため、taskが
        # 違えば同じURLが両方とも根拠として残る(deduplicated_evidence_count==0)。
        # 採用件数(internal_evidence_count/external_evidence_count)はtask単位の
        # fieldではなくEvidenceCollectionOutcome.reviewへ移動した。
        [item.source_ref for item in outcome.evidence],
        outcome.deduplicated_evidence_count,
        (review.review, review.internal_evidence_count, review.external_evidence_count),
    ) == (
        [task.research_goal for task in tasks],
        4,
        2,
        3,
        [
            (0, "succeeded", ["q1"], 1),
            (1, "succeeded", ["q2"], 1),
        ],
        ["0-0", "1-1"],
        0,
        ("succeeded", 0, 2),
    )


@pytest.mark.asyncio
async def test_collection_events_are_per_task_causal_with_their_contract_payloads() -> (
    None
):
    """収集event(queries_generated → candidates_fetched)がtaskごとに正しい

    順序・payloadで出ることを保証する(不変条件ごとに所有テストを決める)。
    S1でreviewerはRun単位1回になり、全taskの収集完了を待ってから走るため、
    evidence_review.selectedは両task分がまとめて精査成功後にRun単位1本でしか
    発火しない(task0が完結してからtask1が始まる、というper-task逐次因果は
    成立しない)。evidence_review.selectedの発火本数・payload形は
    tests/agent/running/test_evidence_review_run_scope.py::
    test_selected_event_fires_once_for_the_whole_run_without_task_index が
    正本のため、ここでは重複して主張しない。
    """
    events = _Events()
    query_runtime = ScriptedAgentRuntime([_query_draft(["q1"]), _query_draft(["q2"])])
    reviewer_runtime = ScriptedAgentRuntime([_review_draft([]), _review_draft([])])
    runner, _, _ = _runner(
        tasks=[_task("first"), _task("second")],
        runtime=_runtime(
            query_runtime=query_runtime,
            reviewer_runtime=reviewer_runtime,
            tool=_Tool(
                {
                    "q1": [_candidate("https://example.com/q1")],
                    "q2": [_candidate("https://example.com/q2")],
                }
            ),
        ),
        events=events,
        requested_agent_count=1,
    )

    await _run(runner)

    collection_events = [
        event.model_dump()
        for event in _external_search_events(events.events)
        if event.type
        in {
            "evidence_collection.external_search_queries_generated",
            "evidence_collection.external_search_candidates_fetched",
        }
    ]
    assert collection_events == [
        {
            "type": "evidence_collection.external_search_queries_generated",
            "task_index": 0,
            "queries": ["q1"],
        },
        {
            "type": "evidence_collection.external_search_candidates_fetched",
            "task_index": 0,
            "candidate_count": 1,
        },
        {
            "type": "evidence_collection.external_search_queries_generated",
            "task_index": 1,
            "queries": ["q2"],
        },
        {
            "type": "evidence_collection.external_search_candidates_fetched",
            "task_index": 1,
            "candidate_count": 1,
        },
    ]


@pytest.mark.asyncio
async def test_evidence_selected_event_count_is_internal_plus_external() -> None:
    """evidence_review.selected.evidence_countは内部採用数と外部採用数の合算であることを保証する。"""
    events = _Events()
    query_runtime = ScriptedAgentRuntime([_query_draft(["q1"])])
    reviewer_runtime = ScriptedAgentRuntime(
        [
            _review_draft(
                [
                    {
                        "candidate_index": 0,
                        "claim": "internal claim",
                        "why_selected": "why",
                    },
                    {
                        "candidate_index": 1,
                        "claim": "external claim",
                        "why_selected": "why",
                    },
                ]
            )
        ]
    )
    answerer = _EvidenceAnswerer()
    factory = _Factory(
        [
            _runtime(
                query_runtime=query_runtime,
                reviewer_runtime=reviewer_runtime,
                tool=_Tool({"q1": [_candidate("https://example.com/q1")]}),
            )
        ]
    )
    phases = AnsweringPhases(
        planner=_Planner(_plan([_task("combined evidence")])),
        collector=NewsCollector(
            researcher=Researcher(
                internal_search=_OneInternalHitSearch(), events=events
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

    selected_events = [
        event.model_dump()
        for event in _external_search_events(events.events)
        if event.type == "evidence_review.selected"
    ]
    assert selected_events == [
        {
            "type": "evidence_review.selected",
            "evidence_count": 2,
        }
    ]


@pytest.mark.asyncio
async def test_external_pipeline_is_a_noop_for_events_when_reporter_is_none() -> None:
    runner, answerer, _ = _runner(
        tasks=[_task("no reporter")],
        runtime=_runtime(
            query_runtime=ScriptedAgentRuntime([_query_draft(["q"])]),
            reviewer_runtime=ScriptedAgentRuntime([_review_draft([])]),
            tool=_Tool({"q": [_candidate("https://example.com/q")]}),
        ),
        events=None,
    )

    await _run(runner)

    assert answerer.calls == [[]]


@pytest.mark.asyncio
async def test_requested_count_limits_only_external_task_parallelism() -> None:
    release = asyncio.Event()
    query_runtime = _ParallelQueryRuntime(release=release)
    runner, _, _ = _runner(
        tasks=[_task("first"), _task("second"), _task("third")],
        runtime=_runtime(
            query_runtime=query_runtime,
            reviewer_runtime=ScriptedAgentRuntime([]),
            tool=_Tool(),
        ),
        requested_agent_count=2,
    )
    running = asyncio.create_task(_run(runner))

    try:
        await asyncio.wait_for(query_runtime.two_started.wait(), timeout=0.5)
        assert query_runtime.peak == 2
    finally:
        release.set()
        await asyncio.wait_for(running, timeout=0.5)


@pytest.mark.asyncio
async def test_outer_cancellation_cancels_and_joins_all_started_external_tasks() -> (
    None
):
    tasks = [_task("first blocking task"), _task("second blocking task")]
    timeline: list[str] = []
    query_runtime = _AllTasksBlockingRuntime(
        task_count=len(tasks),
        timeline=timeline,
    )
    runner, answerer, factory = _runner(
        tasks=tasks,
        runtime=_runtime(
            query_runtime=query_runtime,
            reviewer_runtime=ScriptedAgentRuntime([]),
            tool=_Tool(),
        ),
        requested_agent_count=len(tasks),
        timeline=timeline,
    )
    running = asyncio.create_task(_run(runner))

    await asyncio.wait_for(query_runtime.all_tasks_started.wait(), timeout=0.5)
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(running, timeout=0.5)

    assert (
        query_runtime.started_count,
        query_runtime.cancelled_count,
        query_runtime.finished_count,
        query_runtime.all_tasks_finished.is_set(),
        answerer.calls,
        factory.scopes[0].exit_calls,
        timeline.count("external_task.finished"),
        max(
            index
            for index, event in enumerate(timeline)
            if event == "external_task.finished"
        )
        < timeline.index("scope.exit"),
    ) == (2, 2, 2, True, [], 1, 2, True)


@pytest.mark.asyncio
async def test_classified_task_failure_does_not_cancel_its_sibling() -> None:
    failed = AgentResponseInvalidError(AgentResponseDefect.RESPONSE_NOT_JSON)
    query_runtime = ScriptedAgentRuntime([failed, _query_draft(["q"])])
    reviewer_runtime = ScriptedAgentRuntime(
        [
            _review_draft(
                [{"candidate_index": 0, "claim": "claim", "why_selected": "why"}]
            )
        ]
    )
    runner, answerer, _ = _runner(
        tasks=[_task("failed"), _task("succeeds")],
        runtime=_runtime(
            query_runtime=query_runtime,
            reviewer_runtime=reviewer_runtime,
            tool=_Tool({"q": [_candidate("https://example.com/q")]}),
        ),
        requested_agent_count=1,
    )

    await _run(runner)

    assert [
        (item.source.title, item.source.evidence_claim) for item in answerer.calls[0]
    ] == [("q", "claim")]


@pytest.mark.asyncio
async def test_unclassified_task_failure_joins_sibling_before_scope_close() -> None:
    error = RuntimeError("UNCLASSIFIED_TASK_ERROR")
    timeline: list[str] = []
    query_runtime = _TaskFailureAfterSiblingStartsRuntime(
        error=error, timeline=timeline
    )
    runner, _, factory = _runner(
        tasks=[_task("failing"), _task("blocking")],
        runtime=_runtime(
            query_runtime=query_runtime,
            reviewer_runtime=ScriptedAgentRuntime([]),
            tool=_Tool(),
        ),
        requested_agent_count=2,
        timeline=timeline,
    )

    with pytest.raises(RuntimeError) as raised:
        await asyncio.wait_for(_run(runner), timeout=0.5)

    assert (
        raised.value is error,
        query_runtime.sibling_cancelled,
        query_runtime.sibling_finished.is_set(),
        factory.scopes[0].exited,
        timeline.index("sibling.finished") < timeline.index("scope.exit"),
    ) == (True, True, True, True, True)


@pytest.mark.asyncio
async def test_unclassified_query_failure_joins_sibling_before_reraise() -> None:
    error = RuntimeError("UNCLASSIFIED_QUERY_ERROR")
    tool = _QueryFailureAfterSiblingStartsTool(error=error)
    runner, _, factory = _runner(
        tasks=[_task("query siblings")],
        runtime=_runtime(
            query_runtime=ScriptedAgentRuntime([_query_draft(["failing", "blocking"])]),
            reviewer_runtime=ScriptedAgentRuntime([]),
            tool=tool,
        ),
    )

    with pytest.raises(RuntimeError) as raised:
        await asyncio.wait_for(_run(runner), timeout=0.5)

    assert (
        raised.value is error,
        tool.sibling_cancelled,
        tool.sibling_finished.is_set(),
        factory.scopes[0].exited,
    ) == (True, True, True, True)


@pytest.mark.asyncio
async def test_cross_task_same_url_both_kept_and_scope_is_fresh_per_run() -> None:
    """S1(合流と重複排除): 外部根拠のURL重複排除は廃止されたため、taskが違えば

    同じURLが両方とも根拠として残る(旧: URL先勝ちdedupで片方だけが残っていた)。
    reviewerはRun単位1回のため、統合index空間(仮定: task昇順)の0,1を1つの
    draftで選ばせる(task単位で呼ぶ旧経路が残っていても2件目のcallが
    script枯渇crashにならないよう空draftを足す)。
    """
    tasks = [_task("first"), _task("second")]

    def _reviewer_runtime() -> ScriptedAgentRuntime:
        return ScriptedAgentRuntime(
            [
                _review_draft(
                    [
                        {"candidate_index": 0, "claim": "first", "why_selected": "why"},
                        {
                            "candidate_index": 1,
                            "claim": "second",
                            "why_selected": "why",
                        },
                    ]
                ),
                _review_draft([]),
            ]
        )

    def _tool() -> _Tool:
        return _Tool(
            {
                "q1": [_candidate("https://example.com/shared", title="first")],
                "q2": [_candidate("https://example.com/shared", title="second")],
            }
        )

    first_runtime = _runtime(
        query_runtime=ScriptedAgentRuntime(
            [_query_draft(["q1"]), _query_draft(["q2"])]
        ),
        reviewer_runtime=_reviewer_runtime(),
        tool=_tool(),
    )
    second_runtime = _runtime(
        query_runtime=ScriptedAgentRuntime(
            [_query_draft(["q1"]), _query_draft(["q2"])]
        ),
        reviewer_runtime=_reviewer_runtime(),
        tool=_tool(),
    )
    answerer = _EvidenceAnswerer()
    factory = _Factory([first_runtime, second_runtime])
    phases = AnsweringPhases(
        planner=_Planner(_plan(tasks)),
        collector=NewsCollector(
            researcher=Researcher(internal_search=_EmptyInternalSearch()),
            requested_agent_count=1,
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
    await _run(runner)

    assert (
        [sorted(item.source.title for item in evidence) for evidence in answerer.calls],
        len(factory.scopes),
        factory.scopes[0] is not factory.scopes[1],
        [scope.exit_calls for scope in factory.scopes],
    ) == ([["first", "second"], ["first", "second"]], 2, True, [1, 1])


@pytest.mark.asyncio
async def test_query_timeout_is_classified_without_reviewer() -> None:
    reviewer_runtime = ScriptedAgentRuntime([])
    runner, _, _ = _runner(
        tasks=[_task("timeout")],
        runtime=_runtime(
            query_runtime=ScriptedAgentRuntime(
                [AIProviderNetworkError(reason=DeepSeekStateReason.TIMEOUT)]
            ),
            reviewer_runtime=reviewer_runtime,
            tool=_Tool(),
        ),
    )

    await _run(runner)

    assert reviewer_runtime.calls == []


@pytest.mark.asyncio
async def test_query_timeout_backstop_cancels_the_runtime_and_reports_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed_timeouts = _record_and_shorten_pipeline_timeouts(monkeypatch)
    captured = _capture_external_outcome(monkeypatch)
    query_runtime = _NeverCompletingRuntime()
    runner, _, _ = _runner(
        tasks=[_task("query timeout")],
        runtime=_runtime(
            query_runtime=query_runtime,
            reviewer_runtime=ScriptedAgentRuntime([]),
            tool=_Tool(),
        ),
    )

    await asyncio.wait_for(_run(runner), timeout=0.5)

    report = captured[0].task_reports[0]
    assert (
        query_runtime.cancelled,
        report.external_collection,
        # S1: reviewはtask単位のfieldではなくoutcome.reviewへ移動した。
        captured[0].review.review,
        report.generated_queries,
        observed_timeouts.count(30),
    ) == (True, "query_generation_failed", "skipped_empty", [], 1)


@pytest.mark.asyncio
async def test_provider_timeout_backstop_cancels_tool_and_skips_reviewer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed_timeouts = _record_and_shorten_pipeline_timeouts(monkeypatch)
    captured = _capture_external_outcome(monkeypatch)
    started = asyncio.Event()
    tool = _Tool(started=started, release=asyncio.Event())
    reviewer_runtime = ScriptedAgentRuntime([])
    runner, _, _ = _runner(
        tasks=[_task("provider timeout")],
        runtime=_runtime(
            query_runtime=ScriptedAgentRuntime([_query_draft(["q"])]),
            reviewer_runtime=reviewer_runtime,
            tool=tool,
        ),
    )

    await asyncio.wait_for(_run(runner), timeout=0.5)

    report = captured[0].task_reports[0]
    assert (
        started.is_set(),
        tool.cancelled,
        reviewer_runtime.calls,
        report.external_collection,
        # S1: reviewはtask単位のfieldではなくoutcome.reviewへ移動した。
        captured[0].review.review,
        report.provider_failed_query_count,
        observed_timeouts.count(15),
    ) == (True, True, [], "provider_failed", "skipped_empty", 1, 1)


# reviewerのtimeout backstop attempt/retry契約は
# tests/agent/evidence_review/test_reviewer.py が正本。
