"""AnsweringRunner が所有する external Query -> Tool -> Evidence Reviewer 契約。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from datetime import UTC, date, datetime
from typing import Any

import pytest
from logfire.testing import CaptureLogfire
from structlog.testing import capture_logs

from app.agent.evidence_collection import EvidenceCollectionService
from app.agent.evidence_collection.external_search import (
    ExternalSearch,
    ExternalSearchDateFilter,
    ExternalSearchHit,
    ExternalSearchProviderError,
)
from app.agent.evidence_collection.external_search import (
    service as external_search_service_module,
)
from app.agent.evidence_collection.external_search.contract import (
    EXTERNAL_SEARCH_HITS_PER_QUERY,
)
from app.agent.evidence_collection.internal_search import (
    InternalArticleSearchHit,
)
from app.agent.evidence_review import EvidenceReviewer, EvidenceRunCompleted
from app.agent.planning.contract import (
    ExternalResearchTask,
    PlanningRequest,
    ResearchTask,
    SearchPlan,
    TargetTimeWindow,
)
from app.agent.running import AnsweringPhases, AnsweringRunner
from app.agent.runtime.contract import AgentResponseDefect, AgentResponseInvalidError
from app.analysis.ai_provider_errors import AIProviderNetworkError
from app.analysis.deepseek_error_translator import DeepSeekStateReason
from tests.agent.running._harness import (
    AS_OF,
    ExternalScopes,
    fixed_scope,
    internal_hit,
)
from tests.agent.running._harness import (
    DEFAULT_TARGET_TIME_WINDOW as _DEFAULT_TARGET_TIME_WINDOW,
)
from tests.agent.running._harness import (
    Events as _Events,
)
from tests.agent.running._harness import (
    EvidenceAnswerer as _EvidenceAnswerer,
)
from tests.agent.running._harness import (
    Preparer as _Preparer,
)
from tests.agent.running._harness import (
    UnreachableDirectAnswerer as _UnreachableDirectAnswerer,
)
from tests.agent.running._harness import (
    capture_external_outcome as _capture_external_outcome,
)
from tests.agent.running._harness import (
    execute_run as _run,
)
from tests.agent.running._harness import (
    external_hit as _hit,
)
from tests.agent.running._harness import (
    external_research_runtime as _runtime,
)
from tests.agent.running._harness import (
    external_task as _task,
)
from tests.agent.running._harness import (
    query_draft as _query_draft,
)
from tests.agent.running._harness import (
    review_draft as _review_draft,
)
from tests.agent.running._input_safety import AllowInputSafetyChecker
from tests.agent.runtime._fakes import ScriptedAgentRuntime
from tests.logfire._metric_helpers import collected_metrics

_TIME_FILTER_METRIC = "external_search_time_filter_resolution_total"


def _task_reports(captured: Any) -> list[Any]:
    return [task.report for task in captured.collected_news.tasks]


def _completed_evidence_run(captured: Any) -> EvidenceRunCompleted:
    evidence_run = captured.evidence_run
    assert isinstance(evidence_run, EvidenceRunCompleted)
    return evidence_run


def _plan(
    tasks: list[ExternalResearchTask],
    *,
    target_time_window: TargetTimeWindow | None = _DEFAULT_TARGET_TIME_WINDOW,
) -> SearchPlan:
    return SearchPlan(
        research_tasks=[
            ResearchTask(
                research_goal=task.research_goal,
                article_search_queries=["NVIDIA"],
            )
            for task in tasks
        ],
        target_time_window=target_time_window,
    )


class _Planner:
    def __init__(self, plan: Any) -> None:
        self.plan_result = plan
        self.calls: list[PlanningRequest] = []

    async def plan(self, request: PlanningRequest) -> Any:
        self.calls.append(request)
        return self.plan_result


class _EmptyInternalSearch:
    async def search(self, queries: object) -> list[object]:
        del queries
        return []


def _internal_hit(*, assessment_id: int, title: str) -> InternalArticleSearchHit:
    # このファイルのfixture採番: curation_id は assessment_id - 1000 で導出する。
    return internal_hit(
        assessment_id=assessment_id,
        curation_id=assessment_id - 1000,
        title=title,
    )


class _OneInternalHitSearch:
    """internal+externalの合算件数を確かめるため、内部hitを1件返す。"""

    async def search(self, queries: object) -> list[InternalArticleSearchHit]:
        del queries
        return [_internal_hit(assessment_id=2001, title="internal hit")]


class _Scope(AbstractAsyncContextManager[ExternalSearch]):
    def __init__(self, external_search: ExternalSearch, timeline: list[str]) -> None:
        self._external_search = external_search
        self._timeline = timeline
        self.entered = False
        self.exited = False
        self.exit_calls = 0

    async def __aenter__(self) -> ExternalSearch:
        self.entered = True
        self._timeline.append("scope.enter")
        return self._external_search

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
        searches: Sequence[ExternalSearch],
        *,
        timeline: list[str] | None = None,
    ) -> None:
        self._searches = list(searches)
        self.timeline = timeline if timeline is not None else []
        self.scopes: list[_Scope] = []

    def __call__(self) -> _Scope:
        scope = _Scope(self._searches.pop(0), self.timeline)
        self.scopes.append(scope)
        return scope


class _FakeExternalSearchGateway:
    def __init__(
        self,
        results_by_query: dict[str, list[ExternalSearchHit]] | None = None,
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

    async def search(self, request: Any) -> list[ExternalSearchHit]:
        self.calls.append(request)
        try:
            if self._started is not None:
                self._started.set()
            if self._release is not None:
                await self._release.wait()
            if request.query in self._errors_by_query:
                raise self._errors_by_query[request.query]
            return list(self._results_by_query.get(request.query, []))
        except asyncio.CancelledError:
            self.cancelled = True
            raise


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

    async def call(self, agent: object, input: Any, *, attempt_number: int) -> Any:
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

    async def call(self, agent: object, input: Any, *, attempt_number: int) -> Any:
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

    async def call(self, agent: object, input: object, *, attempt_number: int) -> Any:
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

    async def call(self, agent: object, input: Any, *, attempt_number: int) -> Any:
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


class _QueryFailureAfterSiblingStartsGateway(_FakeExternalSearchGateway):
    def __init__(self, *, error: BaseException) -> None:
        super().__init__()
        self._error = error
        self.sibling_started = asyncio.Event()
        self.sibling_finished = asyncio.Event()
        self.sibling_cancelled = False

    async def search(self, request: Any) -> list[ExternalSearchHit]:
        self.calls.append(request)
        if request.query == "failing":
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


def _runner(
    *,
    tasks: list[ExternalResearchTask],
    runtime: ExternalScopes,
    events: _Events | None = None,
    requested_agent_count: int | None = None,
    timeline: list[str] | None = None,
    target_time_window: TargetTimeWindow | None = _DEFAULT_TARGET_TIME_WINDOW,
) -> tuple[AnsweringRunner, _EvidenceAnswerer, _Factory]:
    answerer = _EvidenceAnswerer()
    factory = _Factory([runtime.external_search], timeline=timeline)
    phases = AnsweringPhases(
        planner=_Planner(
            _plan(tasks, target_time_window=target_time_window),
        ),
        collector=EvidenceCollectionService(
            internal_search=_EmptyInternalSearch(),
            events=events,
            external_search_scope_factory=factory,
            requested_agent_count=requested_agent_count,
        ),
        direct_answerer=_UnreachableDirectAnswerer(),
        evidence_answerer=answerer,
        reviewer=EvidenceReviewer(
            runtime_scope_factory=fixed_scope(runtime.reviewer_runtime),
        ),
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
async def test_external_pipeline_normalizes_and_caps_generated_queries() -> None:
    """query正規化・重複除去・件数capの配線。URL非到達の正本はrun_scope B5。"""
    long_query = "x" * 205
    query_runtime = ScriptedAgentRuntime(
        [_query_draft(["  normalized  ", "normalized", long_query, "third", "fourth"])]
    )
    reviewer_runtime = ScriptedAgentRuntime(
        [_review_draft([{"option_index": 1, "claim": "claim", "why_selected": "why"}])]
    )
    gateway = _FakeExternalSearchGateway(
        {
            "normalized": [_hit("https://example.com/first")],
            "x" * 200: [_hit("https://example.com/second")],
            "third": [_hit("https://example.com/third")],
        }
    )
    runner, answerer, _ = _runner(
        tasks=[_task("collect evidence")],
        runtime=_runtime(
            query_runtime=query_runtime,
            reviewer_runtime=reviewer_runtime,
            gateway=gateway,
        ),
    )

    result = await _run(runner)

    assert [call.query for call in gateway.calls] == [
        "normalized",
        "x" * 200,
        "third",
    ]
    assert all(call.limit == 10 for call in gateway.calls)
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
    gateway = _FakeExternalSearchGateway(
        {
            "first": [_hit("https://example.com/first")],
            "second": [_hit("https://example.com/second")],
        }
    )
    runner, _, _ = _runner(
        tasks=[_task("first task"), _task("second task")],
        runtime=_runtime(
            query_runtime=query_runtime,
            reviewer_runtime=ScriptedAgentRuntime(
                [_review_draft([]), _review_draft([])]
            ),
            gateway=gateway,
        ),
        target_time_window=target_time_window,
    )

    await _run(runner)

    assert (
        [call.date_filter for call in gateway.calls],
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
    gateway = _FakeExternalSearchGateway(
        {"query": [_hit("https://example.com/no-filter")]}
    )
    runner, _, _ = _runner(
        tasks=[_task("no publication filter")],
        runtime=_runtime(
            query_runtime=ScriptedAgentRuntime([_query_draft(["query"])]),
            reviewer_runtime=ScriptedAgentRuntime([_review_draft([])]),
            gateway=gateway,
        ),
        target_time_window=None,
    )

    await _run(runner)

    assert [call.date_filter for call in gateway.calls] == [None]


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
            4,
            id="resolution-failed",
        ),
    ],
)
async def test_external_runner_resolves_target_time_window_once_per_branch(
    monkeypatch: pytest.MonkeyPatch,
    target_time_window: TargetTimeWindow | None,
    expected_tool_call_count: int,
) -> None:
    original_resolver = (
        external_search_service_module.resolve_external_search_date_filter
    )
    resolver_calls: list[tuple[TargetTimeWindow | None, datetime]] = []

    def spy(
        target: TargetTimeWindow | None,
        *,
        as_of: datetime,
    ) -> ExternalSearchDateFilter | None:
        resolver_calls.append((target, as_of))
        return original_resolver(target, as_of=as_of)

    monkeypatch.setattr(
        external_search_service_module,
        "resolve_external_search_date_filter",
        spy,
    )
    tasks = [_task("first period task"), _task("second period task")]
    gateway = _FakeExternalSearchGateway()
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
            gateway=gateway,
        ),
        target_time_window=target_time_window,
    )

    await _run(runner)

    assert (
        resolver_calls,
        len(tasks),
        len(gateway.calls),
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
    original_resolver = (
        external_search_service_module.resolve_external_search_date_filter
    )
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
        external_search_service_module,
        "resolve_external_search_date_filter",
        spy,
    )
    captured = _capture_external_outcome(monkeypatch)
    events = _Events()
    query_runtime = ScriptedAgentRuntime([])
    reviewer_runtime = ScriptedAgentRuntime([])
    gateway = _FakeExternalSearchGateway()
    runner, answerer, factory = _runner(
        tasks=[_task("naive as_of は分類しない")],
        runtime=_runtime(
            query_runtime=query_runtime,
            reviewer_runtime=reviewer_runtime,
            gateway=gateway,
        ),
        events=events,
    )

    with capture_logs() as logs, pytest.raises(ValueError):
        await _run(runner, as_of=naive_as_of)
    metrics = collected_metrics(capfire)

    assert (
        resolver_calls,
        len(factory.scopes),
        query_runtime.calls,
        reviewer_runtime.calls,
        gateway.calls,
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
        1,
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
    gateway = _FakeExternalSearchGateway()
    runner, _, factory = _runner(
        tasks=[_task("期間計測")],
        runtime=_runtime(
            query_runtime=ScriptedAgentRuntime([_query_draft(["metric query"])]),
            reviewer_runtime=ScriptedAgentRuntime([]),
            gateway=gateway,
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
async def test_time_filter_resolution_failure_still_searches_without_date_filter(
    capfire: CaptureLogfire,
    monkeypatch: pytest.MonkeyPatch,
    target_time_window: TargetTimeWindow,
    expected_reason: str,
) -> None:
    captured = _capture_external_outcome(monkeypatch)
    events = _Events()
    query_runtime = ScriptedAgentRuntime(
        [_query_draft(["first-q"]), _query_draft(["second-q"])]
    )
    reviewer_runtime = ScriptedAgentRuntime([_review_draft([])])
    gateway = _FakeExternalSearchGateway(
        {
            "first-q": [_hit("https://example.com/first")],
            "second-q": [_hit("https://example.com/second")],
        }
    )
    tasks = [_task("first closed task"), _task("second closed task")]
    runner, _answerer, factory = _runner(
        tasks=tasks,
        runtime=_runtime(
            query_runtime=query_runtime,
            reviewer_runtime=reviewer_runtime,
            gateway=gateway,
        ),
        events=events,
        target_time_window=target_time_window,
        requested_agent_count=1,
    )

    with capture_logs() as logs:
        await _run(runner)
    metrics = collected_metrics(capfire)
    reports = _task_reports(captured[0])

    assert (
        len(factory.scopes),
        [call.input.target_time_window for call in query_runtime.calls],
        [call.date_filter for call in gateway.calls],
        [
            (
                report.task_index,
                report.external_collection,
                report.generated_queries,
                report.external_hit_count,
            )
            for report in reports
        ],
        _time_filter_metric_points(metrics),
        [
            entry
            for entry in logs
            if entry.get("event") == "external_search_time_filter_failed"
        ],
    ) == (
        1,
        [target_time_window, target_time_window],
        [None, None],
        [
            (0, "succeeded", ["first-q"], 1),
            (1, "succeeded", ["second-q"], 1),
        ],
        [(1, {"result": "failed", "reason": expected_reason})],
        [
            {
                "reason": expected_reason,
                "event": "external_search_time_filter_failed",
                "log_level": "warning",
            }
        ],
    )


@pytest.mark.asyncio
async def test_provider_result_cap_is_applied_before_external_hits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = _capture_external_outcome(monkeypatch)
    reviewer_runtime = ScriptedAgentRuntime([_review_draft([])])
    runner, _, _ = _runner(
        tasks=[_task("provider result cap")],
        runtime=_runtime(
            query_runtime=ScriptedAgentRuntime([_query_draft(["q"])]),
            reviewer_runtime=reviewer_runtime,
            gateway=_FakeExternalSearchGateway(
                {
                    "q": [
                        _hit(f"https://example.com/hit-{index}")
                        for index in range(EXTERNAL_SEARCH_HITS_PER_QUERY + 3)
                    ]
                }
            ),
        ),
    )

    await _run(runner)

    options = reviewer_runtime.calls[0].input.task_groups[0].options
    assert (
        len(options),
        options[-1].title,
        _task_reports(captured[0])[0].external_hit_count,
    ) == (
        EXTERNAL_SEARCH_HITS_PER_QUERY,
        f"hit-{EXTERNAL_SEARCH_HITS_PER_QUERY - 1}",
        EXTERNAL_SEARCH_HITS_PER_QUERY,
    )


@pytest.mark.asyncio
async def test_classified_query_failure_never_starts_tool_or_reviewer() -> None:
    query_runtime = ScriptedAgentRuntime(
        [AgentResponseInvalidError(AgentResponseDefect.RESPONSE_NOT_JSON)]
    )
    reviewer_runtime = ScriptedAgentRuntime([])
    gateway = _FakeExternalSearchGateway()
    runner, answerer, factory = _runner(
        tasks=[_task("invalid query")],
        runtime=_runtime(
            query_runtime=query_runtime,
            reviewer_runtime=reviewer_runtime,
            gateway=gateway,
        ),
    )

    await _run(runner)

    assert (
        gateway.calls,
        reviewer_runtime.calls,
        answerer.calls,
        factory.scopes[0].exit_calls,
    ) == ([], [], [[]], 1)


@pytest.mark.asyncio
async def test_partial_provider_failure_continues_but_all_failure_skips_reviewer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = _capture_external_outcome(monkeypatch)
    provider_error = ExternalSearchProviderError(reason="external_search_http_error")
    query_runtime = ScriptedAgentRuntime(
        [_query_draft(["good", "bad"]), _query_draft(["bad"])]
    )
    reviewer_runtime = ScriptedAgentRuntime([_review_draft([])])
    gateway = _FakeExternalSearchGateway(
        {"good": [_hit("https://example.com/good")]},
        errors_by_query={"bad": provider_error},
    )
    runner, answerer, _ = _runner(
        tasks=[_task("partial failure"), _task("complete failure")],
        runtime=_runtime(
            query_runtime=query_runtime,
            reviewer_runtime=reviewer_runtime,
            gateway=gateway,
        ),
        requested_agent_count=1,
    )

    await _run(runner)

    assert (
        [call.query for call in gateway.calls],
        len(reviewer_runtime.calls),
        answerer.calls,
        [
            (
                report.external_collection,
                report.provider_failed_query_count,
                report.external_hit_count,
            )
            for report in _task_reports(captured[0])
        ],
        isinstance(captured[0].evidence_run, EvidenceRunCompleted),
    ) == (
        ["good", "bad", "bad"],
        1,
        [[]],
        [
            ("succeeded", 1, 1),
            ("provider_failed", 1, 0),
        ],
        True,
    )


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
            # 唯一の選択肢が0、task1の唯一の選択肢が1になる。task単位で呼ぶ旧経路が
            # 残っていても2件目のcallがscript枯渇crashにならないよう空draftを足す。
            reviewer_runtime=ScriptedAgentRuntime(
                [
                    _review_draft(
                        [
                            {
                                "option_index": 0,
                                "claim": "first claim",
                                "why_selected": "why",
                            },
                            {
                                "option_index": 1,
                                "claim": "second claim",
                                "why_selected": "why",
                            },
                        ]
                    ),
                    _review_draft([]),
                ]
            ),
            gateway=_FakeExternalSearchGateway(
                {
                    "q1": [_hit("https://example.com/shared", title="first")],
                    "q2": [_hit("https://example.com/shared", title="second")],
                }
            ),
        ),
        requested_agent_count=4,
    )

    await _run(runner)

    outcome = captured[0]
    reports = _task_reports(outcome)
    evidence_run = _completed_evidence_run(outcome)
    assert (
        [report.research_goal for report in reports],
        outcome.collected_news.requested_agent_count,
        outcome.collected_news.effective_agent_count,
        [
            (
                report.task_index,
                report.external_collection,
                report.generated_queries,
                report.external_hit_count,
            )
            for report in reports
        ],
        # 同じURLでもtaskが異なるEvidenceはそれぞれ残す。
        [item.option_index for item in evidence_run.answer_evidence.external_evidence],
        evidence_run.review_missing,
    ) == (
        [task.research_goal for task in tasks],
        4,
        2,
        [
            (0, "succeeded", ["q1"], 1),
            (1, "succeeded", ["q2"], 1),
        ],
        [0, 1],
        (),
    )


@pytest.mark.asyncio
async def test_collection_events_are_per_task_causal_with_their_contract_payloads() -> (
    None
):
    """収集event(queries_generated → hits_fetched)がtaskごとに正しい

    順序・payloadで出ることを保証する(不変条件ごとに所有テストを決める)。
    S1でreviewerはRun単位1回になり、全taskの収集完了を待ってから走るため、
    evidence_review.selectedは両task分がまとめて精査成功後にRun単位1本でしか
    発火しない(task0が完結してからtask1が始まる、というper-task逐次因果は
    成立しない)。evidence_review.selectedの発火本数・payload形は
    tests/agent/running/test_evidence_review.py::
    test_selected_event_fires_once_for_the_whole_run_without_task_index が
    正本のため、ここでは重複して主張しない。
    """
    events = _Events()
    query_runtime = ScriptedAgentRuntime([_query_draft(["q1"]), _query_draft(["q2"])])
    reviewer_runtime = ScriptedAgentRuntime([_review_draft([])])
    runner, _, _ = _runner(
        tasks=[_task("first"), _task("second")],
        runtime=_runtime(
            query_runtime=query_runtime,
            reviewer_runtime=reviewer_runtime,
            gateway=_FakeExternalSearchGateway(
                {
                    "q1": [_hit("https://example.com/q1")],
                    "q2": [_hit("https://example.com/q2")],
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
            "evidence_collection.external_search_hits_fetched",
        }
    ]
    assert collection_events == [
        {
            "type": "evidence_collection.external_search_queries_generated",
            "task_index": 0,
            "queries": ["q1"],
        },
        {
            "type": "evidence_collection.external_search_hits_fetched",
            "task_index": 0,
            "hit_count": 1,
        },
        {
            "type": "evidence_collection.external_search_queries_generated",
            "task_index": 1,
            "queries": ["q2"],
        },
        {
            "type": "evidence_collection.external_search_hits_fetched",
            "task_index": 1,
            "hit_count": 1,
        },
    ]


@pytest.mark.asyncio
async def test_external_pipeline_is_a_noop_for_events_when_reporter_is_none() -> None:
    runner, answerer, _ = _runner(
        tasks=[_task("no reporter")],
        runtime=_runtime(
            query_runtime=ScriptedAgentRuntime([_query_draft(["q"])]),
            reviewer_runtime=ScriptedAgentRuntime([_review_draft([])]),
            gateway=_FakeExternalSearchGateway({"q": [_hit("https://example.com/q")]}),
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
            gateway=_FakeExternalSearchGateway(),
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
            gateway=_FakeExternalSearchGateway(),
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
        [_review_draft([{"option_index": 0, "claim": "claim", "why_selected": "why"}])]
    )
    runner, answerer, _ = _runner(
        tasks=[_task("failed"), _task("succeeds")],
        runtime=_runtime(
            query_runtime=query_runtime,
            reviewer_runtime=reviewer_runtime,
            gateway=_FakeExternalSearchGateway({"q": [_hit("https://example.com/q")]}),
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
            gateway=_FakeExternalSearchGateway(),
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
    gateway = _QueryFailureAfterSiblingStartsGateway(error=error)
    runner, _, factory = _runner(
        tasks=[_task("query siblings")],
        runtime=_runtime(
            query_runtime=ScriptedAgentRuntime([_query_draft(["failing", "blocking"])]),
            reviewer_runtime=ScriptedAgentRuntime([]),
            gateway=gateway,
        ),
    )

    with pytest.raises(RuntimeError) as raised:
        await asyncio.wait_for(_run(runner), timeout=0.5)

    assert (
        raised.value is error,
        gateway.sibling_cancelled,
        gateway.sibling_finished.is_set(),
        factory.scopes[0].exited,
    ) == (True, True, True, True)


@pytest.mark.asyncio
async def test_external_scope_is_activated_fresh_per_run() -> None:
    """external runtime scopeはrunごとに新しくactivateされ、終了時に必ずexitする。

    同URL外部ヒットが両方残ること(URL重複排除の廃止)の正本は
    test_evidence_review.py の same_url テストが持つ。
    """
    tasks = [_task("first"), _task("second")]

    def _reviewer_runtime() -> ScriptedAgentRuntime:
        return ScriptedAgentRuntime(
            [
                _review_draft(
                    [
                        {"option_index": 0, "claim": "first", "why_selected": "why"},
                        {
                            "option_index": 1,
                            "claim": "second",
                            "why_selected": "why",
                        },
                    ]
                )
            ]
        )

    def _gateway() -> _FakeExternalSearchGateway:
        return _FakeExternalSearchGateway(
            {
                "q1": [_hit("https://example.com/shared", title="first")],
                "q2": [_hit("https://example.com/shared", title="second")],
            }
        )

    first_runtime = _runtime(
        query_runtime=ScriptedAgentRuntime(
            [_query_draft(["q1"]), _query_draft(["q2"])]
        ),
        reviewer_runtime=_reviewer_runtime(),
        gateway=_gateway(),
    )
    second_runtime = _runtime(
        query_runtime=ScriptedAgentRuntime(
            [_query_draft(["q1"]), _query_draft(["q2"])]
        ),
        reviewer_runtime=_reviewer_runtime(),
        gateway=_gateway(),
    )
    answerer = _EvidenceAnswerer()
    factory = _Factory([first_runtime.external_search, second_runtime.external_search])
    reviewer_runtimes = [
        first_runtime.reviewer_runtime,
        second_runtime.reviewer_runtime,
    ]

    @asynccontextmanager
    async def _reviewer_scope() -> AsyncIterator[object]:
        yield reviewer_runtimes.pop(0)

    phases = AnsweringPhases(
        planner=_Planner(_plan(tasks)),
        collector=EvidenceCollectionService(
            internal_search=_EmptyInternalSearch(),
            external_search_scope_factory=factory,
            requested_agent_count=1,
        ),
        direct_answerer=_UnreachableDirectAnswerer(),
        evidence_answerer=answerer,
        reviewer=EvidenceReviewer(runtime_scope_factory=_reviewer_scope),
    )
    runner = AnsweringRunner(
        input_safety_checker=AllowInputSafetyChecker(),
        context_preparer=_Preparer(),
        phases_factory=lambda: phases,
    )

    await _run(runner)
    await _run(runner)

    assert (
        len(answerer.calls),
        len(factory.scopes),
        factory.scopes[0] is not factory.scopes[1],
        [scope.exit_calls for scope in factory.scopes],
    ) == (2, 2, True, [1, 1])


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
            gateway=_FakeExternalSearchGateway(),
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
            gateway=_FakeExternalSearchGateway(),
        ),
    )

    await asyncio.wait_for(_run(runner), timeout=0.5)

    report = _task_reports(captured[0])[0]
    assert (
        query_runtime.cancelled,
        report.external_collection,
        isinstance(captured[0].evidence_run, EvidenceRunCompleted),
        report.generated_queries,
        observed_timeouts.count(30),
    ) == (True, "query_generation_failed", True, [], 1)


@pytest.mark.asyncio
async def test_provider_timeout_backstop_cancels_tool_and_skips_reviewer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed_timeouts = _record_and_shorten_pipeline_timeouts(monkeypatch)
    captured = _capture_external_outcome(monkeypatch)
    started = asyncio.Event()
    gateway = _FakeExternalSearchGateway(started=started, release=asyncio.Event())
    reviewer_runtime = ScriptedAgentRuntime([])
    runner, _, _ = _runner(
        tasks=[_task("provider timeout")],
        runtime=_runtime(
            query_runtime=ScriptedAgentRuntime([_query_draft(["q"])]),
            reviewer_runtime=reviewer_runtime,
            gateway=gateway,
        ),
    )

    await asyncio.wait_for(_run(runner), timeout=0.5)

    report = _task_reports(captured[0])[0]
    assert (
        started.is_set(),
        gateway.cancelled,
        reviewer_runtime.calls,
        report.external_collection,
        isinstance(captured[0].evidence_run, EvidenceRunCompleted),
        report.provider_failed_query_count,
        observed_timeouts.count(15),
    ) == (True, True, [], "provider_failed", True, 1, 1)


# reviewerのtimeout backstop attempt/retry契約は
# tests/agent/evidence_review/test_reviewer.py が正本。
