"""AnsweringRunner が所有する external Query -> Tool -> Selector 契約。"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Sequence
from contextlib import AbstractAsyncContextManager
from datetime import UTC, date, datetime
from typing import Any
from uuid import UUID

import pytest
from logfire.testing import CaptureLogfire
from structlog.testing import capture_logs

from app.agent.answering.contract import AnsweringRequest
from app.agent.answering.direct_answer.contract import DirectAnswerDraft
from app.agent.answering.evidence_answer.contract import EvidenceAnswerDraft
from app.agent.evidence_collection.external_search import (
    ExternalResearchRuntime,
    ExternalSearchCandidate,
    ExternalSearchDateFilter,
    ExternalSearchProviderError,
)
from app.agent.evidence_collection.external_search.contract import (
    EXTERNAL_SEARCH_CANDIDATES_PER_QUERY,
)
from app.agent.planning.contract import (
    ExternalResearchTask,
    ExternalSearchPlan,
    PlanningRequest,
    TargetTimeWindow,
)
from app.agent.question_context import (
    QuestionContext,
    QuestionContextPreparationResult,
    QuestionContextTelemetry,
)
from app.agent.running import AnsweringPhases, AnsweringRunner, RunContext, RunInput
from app.agent.running import answering_runner as answering_runner_module
from app.agent.runtime.contract import AgentResponseDefect, AgentResponseInvalidError
from app.analysis.ai_provider_errors import AIProviderError, AIProviderNetworkError
from app.analysis.deepseek_error_translator import DeepSeekStateReason
from tests.agent.running._input_safety import AllowInputSafetyChecker
from tests.agent.runtime._fakes import ScriptedAgentRuntime
from tests.logfire._metric_helpers import collected_metrics

RUN_ID = UUID("019bd239-1ed4-7fbb-a336-04fe3c197652")
AS_OF = datetime(2026, 7, 20, 9, 30, tzinfo=UTC)
_DEFAULT_TARGET_TIME_WINDOW = TargetTimeWindow(kind="last_n_days", days=1)
_TIME_FILTER_METRIC = "external_search_time_filter_resolution_total"


def _task(goal: str) -> ExternalResearchTask:
    return ExternalResearchTask(collection_goal=goal)


def _plan(
    tasks: list[ExternalResearchTask],
    *,
    target_time_window: TargetTimeWindow | None = _DEFAULT_TARGET_TIME_WINDOW,
) -> ExternalSearchPlan:
    return ExternalSearchPlan(
        external_research_tasks=tasks,
        target_time_window=target_time_window,
        reason="external evidence is required",
    )


def _query_draft(queries: object) -> Any:
    from app.agent.evidence_collection.external_search.contract import (
        ExternalQueryDraft,
    )

    return ExternalQueryDraft.model_validate({"queries": queries})


def _selection_draft(
    selections: list[dict[str, Any]] | None = None,
    *,
    missing: list[str] | None = None,
) -> Any:
    from app.agent.evidence_collection.external_search.contract import (
        ExternalEvidenceSelectionDraft,
    )

    return ExternalEvidenceSelectionDraft.model_validate(
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
    async def prepare(self, **_kwargs: object) -> QuestionContextPreparationResult:
        return QuestionContextPreparationResult(
            context=QuestionContext(standalone_question="NVIDIA の見通しは？"),
            telemetry=QuestionContextTelemetry(),
        )


class _Planner:
    def __init__(self, plan: ExternalSearchPlan) -> None:
        self.plan_result = plan
        self.calls: list[PlanningRequest] = []

    async def plan(self, request: PlanningRequest) -> ExternalSearchPlan:
        self.calls.append(request)
        return self.plan_result


class _UnreachableInternalSearch:
    async def search_articles(self, queries: object) -> list[object]:
        raise AssertionError(f"internal search must not run: {queries!r}")


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
    ) -> EvidenceAnswerDraft:
        del request, target_time_window
        self.calls.append(list(evidence))
        if evidence:
            return EvidenceAnswerDraft(
                sufficiency="answered",
                answer="根拠に基づく回答です。",
                cited_refs=[item.source.source_ref for item in evidence],
            )
        return EvidenceAnswerDraft(
            sufficiency="insufficient",
            answer="根拠が不足しています。",
            missing_aspects=["根拠が不足しています"],
        )


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
            return _query_draft([input.task.collection_goal])
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
    selector_runtime: object,
    tool: object,
) -> ExternalResearchRuntime:
    return ExternalResearchRuntime(
        query_runtime=query_runtime,  # type: ignore[arg-type]
        selector_runtime=selector_runtime,  # type: ignore[arg-type]
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
        internal_search=_UnreachableInternalSearch(),
        external_runtime_factory=factory,
        direct_answerer=_UnreachableDirectAnswerer(),
        evidence_answerer=answerer,
    )
    return (
        AnsweringRunner(
            input_safety_checker=AllowInputSafetyChecker(),
            context_preparer=_Preparer(),
            phases_factory=lambda: phases,
            events=events,
            requested_external_agent_count=requested_agent_count,
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


def test_answering_runner_accepts_external_event_and_requested_count_dependencies() -> (
    None
):
    parameters = inspect.signature(AnsweringRunner).parameters

    assert (
        tuple(parameters)[-2:],
        parameters["events"].default,
        parameters["requested_external_agent_count"].default,
    ) == (("events", "requested_external_agent_count"), None, None)


def test_answering_phases_owns_runtime_factory_without_external_search_port() -> None:
    assert tuple(AnsweringPhases.__dataclass_fields__) == (
        "planner",
        "internal_search",
        "external_runtime_factory",
        "direct_answerer",
        "evidence_answerer",
    )


@pytest.mark.asyncio
async def test_external_pipeline_normalizes_queries_and_hides_urls_from_selector() -> (
    None
):
    long_query = "x" * 205
    query_runtime = ScriptedAgentRuntime(
        [_query_draft(["  normalized  ", "normalized", long_query, "third", "fourth"])]
    )
    selector_runtime = ScriptedAgentRuntime(
        [
            _selection_draft(
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
            selector_runtime=selector_runtime,
            tool=tool,
        ),
    )

    result = await _run(runner)
    selector_input = selector_runtime.calls[0].input

    assert [call.query for call in tool.calls] == [
        "normalized",
        "x" * 200,
        "third",
    ]
    assert all(call.limit == 10 for call in tool.calls)
    assert all(not hasattr(candidate, "url") for candidate in selector_input.candidates)
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
            selector_runtime=ScriptedAgentRuntime(
                [_selection_draft([]), _selection_draft([])]
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
            selector_runtime=ScriptedAgentRuntime([_selection_draft([])]),
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
    original_resolver = answering_runner_module.resolve_external_search_date_filter
    resolver_calls: list[tuple[TargetTimeWindow | None, datetime]] = []

    def spy(
        target: TargetTimeWindow | None,
        *,
        as_of: datetime,
    ) -> ExternalSearchDateFilter | None:
        resolver_calls.append((target, as_of))
        return original_resolver(target, as_of=as_of)

    monkeypatch.setattr(
        answering_runner_module,
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
            selector_runtime=ScriptedAgentRuntime([]),
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
    original_resolver = answering_runner_module.resolve_external_search_date_filter
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
        answering_runner_module,
        "resolve_external_search_date_filter",
        spy,
    )
    captured = _capture_external_outcome(monkeypatch)
    events = _Events()
    query_runtime = ScriptedAgentRuntime([])
    selector_runtime = ScriptedAgentRuntime([])
    tool = _Tool()
    runner, answerer, factory = _runner(
        tasks=[_task("naive as_of は分類しない")],
        runtime=_runtime(
            query_runtime=query_runtime,
            selector_runtime=selector_runtime,
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
        selector_runtime.calls,
        tool.calls,
        events.events,
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
            selector_runtime=ScriptedAgentRuntime([]),
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
    selector_runtime = ScriptedAgentRuntime([])
    tool = _Tool()
    tasks = [_task("first closed task"), _task("second closed task")]
    runner, answerer, factory = _runner(
        tasks=tasks,
        runtime=_runtime(
            query_runtime=query_runtime,
            selector_runtime=selector_runtime,
            tool=tool,
        ),
        events=events,
        target_time_window=target_time_window,
    )

    with capture_logs() as logs:
        await _run(runner)
    metrics = collected_metrics(capfire)
    reports = captured[0].external_search.task_reports

    assert (
        factory.scopes,
        query_runtime.calls,
        selector_runtime.calls,
        tool.calls,
        events.events,
        answerer.calls,
        [
            (
                report.task_index,
                report.status,
                report.time_filter_failure_reason,
                report.generated_queries,
                report.provider_failed_query_count,
                report.candidate_count,
                report.evidence_count,
                report.dropped_selection_count,
                report.selector_failure_reason,
                report.missing,
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
        [],
        [],
        [],
        [],
        [],
        [[]],
        [
            (
                0,
                "time_filter_failed",
                expected_reason,
                [],
                0,
                0,
                0,
                0,
                None,
                [],
            ),
            (
                1,
                "time_filter_failed",
                expected_reason,
                [],
                0,
                0,
                0,
                0,
                None,
                [],
            ),
        ],
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
    selector_runtime = ScriptedAgentRuntime([_selection_draft([])])
    runner, _, _ = _runner(
        tasks=[_task("provider result cap")],
        runtime=_runtime(
            query_runtime=ScriptedAgentRuntime([_query_draft(["q"])]),
            selector_runtime=selector_runtime,
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

    candidates = selector_runtime.calls[0].input.candidates
    assert (
        len(candidates),
        candidates[-1].title,
        captured[0].external_search.task_reports[0].candidate_count,
    ) == (
        EXTERNAL_SEARCH_CANDIDATES_PER_QUERY,
        f"candidate-{EXTERNAL_SEARCH_CANDIDATES_PER_QUERY - 1}",
        EXTERNAL_SEARCH_CANDIDATES_PER_QUERY,
    )


@pytest.mark.asyncio
async def test_classified_query_failure_never_starts_tool_or_selector() -> None:
    query_runtime = ScriptedAgentRuntime(
        [AgentResponseInvalidError(AgentResponseDefect.RESPONSE_NOT_JSON)]
    )
    selector_runtime = ScriptedAgentRuntime([])
    tool = _Tool()
    runner, answerer, factory = _runner(
        tasks=[_task("invalid query")],
        runtime=_runtime(
            query_runtime=query_runtime,
            selector_runtime=selector_runtime,
            tool=tool,
        ),
    )

    result = await _run(runner)

    assert (
        tool.calls,
        selector_runtime.calls,
        answerer.calls,
        result.final_output.retrieval.collection_failures,
        factory.scopes[0].exit_calls,
    ) == ([], [], [[]], [], 1)


@pytest.mark.asyncio
async def test_partial_provider_failure_continues_but_all_failure_skips_selector(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = _capture_external_outcome(monkeypatch)
    provider_error = ExternalSearchProviderError(reason="tavily_search_http_error")
    query_runtime = ScriptedAgentRuntime(
        [_query_draft(["good", "bad"]), _query_draft(["bad"])]
    )
    selector_runtime = ScriptedAgentRuntime([_selection_draft([])])
    tool = _Tool(
        {"good": [_candidate("https://example.com/good")]},
        errors_by_query={"bad": provider_error},
    )
    runner, answerer, _ = _runner(
        tasks=[_task("partial failure"), _task("complete failure")],
        runtime=_runtime(
            query_runtime=query_runtime,
            selector_runtime=selector_runtime,
            tool=tool,
        ),
        requested_agent_count=1,
    )

    await _run(runner)

    assert (
        [call.query for call in tool.calls],
        len(selector_runtime.calls),
        answerer.calls,
        [
            (
                report.status,
                report.provider_failed_query_count,
                report.candidate_count,
            )
            for report in captured[0].external_search.task_reports
        ],
    ) == (
        ["good", "bad", "bad"],
        1,
        [[]],
        [("succeeded", 1, 1), ("provider_failed", 1, 0)],
    )


@pytest.mark.asyncio
async def test_empty_candidate_pool_skips_selector() -> None:
    selector_runtime = ScriptedAgentRuntime([])
    runner, answerer, _ = _runner(
        tasks=[_task("empty pool")],
        runtime=_runtime(
            query_runtime=ScriptedAgentRuntime([_query_draft(["q"])]),
            selector_runtime=selector_runtime,
            tool=_Tool({"q": []}),
        ),
    )

    result = await _run(runner)

    assert (selector_runtime.calls, answerer.calls, result.final_output.status) == (
        [],
        [[]],
        "insufficient",
    )


@pytest.mark.asyncio
async def test_selector_retries_at_most_twice_with_the_same_typed_input() -> None:
    selector_runtime = ScriptedAgentRuntime(
        [
            AgentResponseInvalidError(AgentResponseDefect.OUTPUT_SCHEMA_MISMATCH),
            _selection_draft(
                [{"candidate_index": 0, "claim": "claim", "why_selected": "why"}]
            ),
        ]
    )
    runner, answerer, _ = _runner(
        tasks=[_task("selector retry")],
        runtime=_runtime(
            query_runtime=ScriptedAgentRuntime([_query_draft(["q"])]),
            selector_runtime=selector_runtime,
            tool=_Tool({"q": [_candidate("https://example.com/q")]}),
        ),
    )

    await _run(runner)

    assert (
        [call.attempt_number for call in selector_runtime.calls],
        selector_runtime.calls[0].input is selector_runtime.calls[1].input,
        len(answerer.calls[0]),
    ) == ([1, 2], True, 1)


@pytest.mark.asyncio
async def test_invalid_selector_draft_retries_without_invalid_evidence() -> None:
    selector_runtime = ScriptedAgentRuntime(
        [
            _selection_draft(
                [{"candidate_index": 0, "claim": "", "why_selected": "why"}]
            ),
            _selection_draft(
                [
                    {"candidate_index": 0, "claim": "first", "why_selected": "why"},
                    {"candidate_index": 0, "claim": "duplicate", "why_selected": "why"},
                    {"candidate_index": 99, "claim": "out", "why_selected": "why"},
                ]
            ),
        ]
    )
    runner, answerer, _ = _runner(
        tasks=[_task("selection validation")],
        runtime=_runtime(
            query_runtime=ScriptedAgentRuntime([_query_draft(["q"])]),
            selector_runtime=selector_runtime,
            tool=_Tool({"q": [_candidate("https://example.com/q")]}),
        ),
    )

    await _run(runner)

    assert (
        [call.attempt_number for call in selector_runtime.calls],
        [(item.source.title, item.source.evidence_claim) for item in answerer.calls[0]],
    ) == ([1, 2], [("q", "first")])


@pytest.mark.asyncio
async def test_selector_unclassified_exception_does_not_retry_or_become_report() -> (
    None
):
    error = RuntimeError("selector unclassified")
    selector_runtime = ScriptedAgentRuntime([error])
    runner, answerer, factory = _runner(
        tasks=[_task("selector unclassified")],
        runtime=_runtime(
            query_runtime=ScriptedAgentRuntime([_query_draft(["q"])]),
            selector_runtime=selector_runtime,
            tool=_Tool({"q": [_candidate("https://example.com/q")]}),
        ),
    )

    with pytest.raises(RuntimeError) as raised:
        await _run(runner)

    assert (
        raised.value is error,
        [call.attempt_number for call in selector_runtime.calls],
        answerer.calls,
        factory.scopes[0].exit_calls,
    ) == (True, [1], [], 1)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure", "expected_reason"),
    [
        pytest.param(
            AgentResponseInvalidError(AgentResponseDefect.RESPONSE_NOT_JSON),
            "response_not_json",
            id="runtime-defect",
        ),
        pytest.param(
            AIProviderNetworkError(reason=DeepSeekStateReason.TIMEOUT),
            "timeout",
            id="provider-reason",
        ),
        pytest.param(AIProviderError(), "selector_error", id="provider-fallback"),
    ],
)
async def test_selector_failure_reason_is_preserved_after_two_attempts(
    monkeypatch: pytest.MonkeyPatch,
    failure: BaseException,
    expected_reason: str,
) -> None:
    captured = _capture_external_outcome(monkeypatch)
    selector_runtime = ScriptedAgentRuntime([failure, failure])
    runner, _, _ = _runner(
        tasks=[_task("selector failure")],
        runtime=_runtime(
            query_runtime=ScriptedAgentRuntime([_query_draft(["q"])]),
            selector_runtime=selector_runtime,
            tool=_Tool({"q": [_candidate("https://example.com/q")]}),
        ),
    )

    await _run(runner)

    report = captured[0].external_search.task_reports[0]
    assert (
        report.status,
        report.selector_failure_reason,
        [call.attempt_number for call in selector_runtime.calls],
    ) == ("selector_failed", expected_reason, [1, 2])


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
            selector_runtime=ScriptedAgentRuntime(
                [
                    _selection_draft(
                        [
                            {
                                "candidate_index": 0,
                                "claim": "first claim",
                                "why_selected": "why",
                            }
                        ]
                    ),
                    _selection_draft(
                        [
                            {
                                "candidate_index": 0,
                                "claim": "second claim",
                                "why_selected": "why",
                            }
                        ]
                    ),
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
    assert (
        outcome.tasks,
        outcome.requested_agent_count,
        outcome.effective_agent_count,
        outcome.hard_agent_limit,
        [
            (
                report.task_index,
                report.status,
                report.generated_queries,
                report.candidate_count,
                report.evidence_count,
                report.dropped_selection_count,
            )
            for report in outcome.task_reports
        ],
        [item.source_ref for item in outcome.evidence],
        outcome.deduplicated_evidence_count,
    ) == (
        tasks,
        4,
        2,
        3,
        [
            (0, "succeeded", ["q1"], 1, 1, 0),
            (1, "succeeded", ["q2"], 1, 1, 0),
        ],
        ["external-0-0"],
        1,
    )


@pytest.mark.asyncio
async def test_events_are_per_task_causal_with_their_contract_payloads() -> None:
    events = _Events()
    query_runtime = ScriptedAgentRuntime([_query_draft(["q1"]), _query_draft(["q2"])])
    selector_runtime = ScriptedAgentRuntime(
        [_selection_draft([]), _selection_draft([])]
    )
    runner, _, _ = _runner(
        tasks=[_task("first"), _task("second")],
        runtime=_runtime(
            query_runtime=query_runtime,
            selector_runtime=selector_runtime,
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

    assert [event.model_dump() for event in events.events] == [
        {
            "type": "external_search.queries_generated",
            "task_index": 0,
            "queries": ["q1"],
        },
        {
            "type": "external_search.candidates_fetched",
            "task_index": 0,
            "candidate_count": 1,
        },
        {
            "type": "external_search.evidence_selected",
            "task_index": 0,
            "evidence_count": 0,
        },
        {
            "type": "external_search.queries_generated",
            "task_index": 1,
            "queries": ["q2"],
        },
        {
            "type": "external_search.candidates_fetched",
            "task_index": 1,
            "candidate_count": 1,
        },
        {
            "type": "external_search.evidence_selected",
            "task_index": 1,
            "evidence_count": 0,
        },
    ]


@pytest.mark.asyncio
async def test_external_pipeline_is_a_noop_for_events_when_reporter_is_none() -> None:
    runner, answerer, _ = _runner(
        tasks=[_task("no reporter")],
        runtime=_runtime(
            query_runtime=ScriptedAgentRuntime([_query_draft(["q"])]),
            selector_runtime=ScriptedAgentRuntime([_selection_draft([])]),
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
            selector_runtime=ScriptedAgentRuntime([]),
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
            selector_runtime=ScriptedAgentRuntime([]),
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
    selector_runtime = ScriptedAgentRuntime(
        [
            _selection_draft(
                [{"candidate_index": 0, "claim": "claim", "why_selected": "why"}]
            )
        ]
    )
    runner, answerer, _ = _runner(
        tasks=[_task("failed"), _task("succeeds")],
        runtime=_runtime(
            query_runtime=query_runtime,
            selector_runtime=selector_runtime,
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
            selector_runtime=ScriptedAgentRuntime([]),
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
            selector_runtime=ScriptedAgentRuntime([]),
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
async def test_cross_task_dedupe_keeps_first_and_scope_is_fresh_per_run() -> None:
    tasks = [_task("first"), _task("second")]
    first_runtime = _runtime(
        query_runtime=ScriptedAgentRuntime(
            [_query_draft(["q1"]), _query_draft(["q2"])]
        ),
        selector_runtime=ScriptedAgentRuntime(
            [
                _selection_draft(
                    [{"candidate_index": 0, "claim": "first", "why_selected": "why"}]
                ),
                _selection_draft(
                    [{"candidate_index": 0, "claim": "second", "why_selected": "why"}]
                ),
            ]
        ),
        tool=_Tool(
            {
                "q1": [_candidate("https://example.com/shared", title="first")],
                "q2": [_candidate("https://example.com/shared", title="second")],
            }
        ),
    )
    second_runtime = _runtime(
        query_runtime=ScriptedAgentRuntime(
            [_query_draft(["q1"]), _query_draft(["q2"])]
        ),
        selector_runtime=ScriptedAgentRuntime(
            [
                _selection_draft(
                    [{"candidate_index": 0, "claim": "first", "why_selected": "why"}]
                ),
                _selection_draft(
                    [{"candidate_index": 0, "claim": "second", "why_selected": "why"}]
                ),
            ]
        ),
        tool=_Tool(
            {
                "q1": [_candidate("https://example.com/shared", title="first")],
                "q2": [_candidate("https://example.com/shared", title="second")],
            }
        ),
    )
    answerer = _EvidenceAnswerer()
    factory = _Factory([first_runtime, second_runtime])
    phases = AnsweringPhases(
        planner=_Planner(_plan(tasks)),
        internal_search=_UnreachableInternalSearch(),
        external_runtime_factory=factory,
        direct_answerer=_UnreachableDirectAnswerer(),
        evidence_answerer=answerer,
    )
    runner = AnsweringRunner(
        input_safety_checker=AllowInputSafetyChecker(),
        context_preparer=_Preparer(),
        phases_factory=lambda: phases,
        requested_external_agent_count=1,
    )

    await _run(runner)
    await _run(runner)

    assert (
        [[item.source.title for item in evidence] for evidence in answerer.calls],
        len(factory.scopes),
        factory.scopes[0] is not factory.scopes[1],
        [scope.exit_calls for scope in factory.scopes],
    ) == ([["first"], ["first"]], 2, True, [1, 1])


@pytest.mark.asyncio
async def test_query_timeout_is_classified_without_selector() -> None:
    selector_runtime = ScriptedAgentRuntime([])
    runner, _, _ = _runner(
        tasks=[_task("timeout")],
        runtime=_runtime(
            query_runtime=ScriptedAgentRuntime(
                [AIProviderNetworkError(reason=DeepSeekStateReason.TIMEOUT)]
            ),
            selector_runtime=selector_runtime,
            tool=_Tool(),
        ),
    )

    await _run(runner)

    assert selector_runtime.calls == []


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
            selector_runtime=ScriptedAgentRuntime([]),
            tool=_Tool(),
        ),
    )

    await asyncio.wait_for(_run(runner), timeout=0.5)

    report = captured[0].external_search.task_reports[0]
    assert (
        query_runtime.cancelled,
        report.status,
        report.generated_queries,
        observed_timeouts.count(30),
    ) == (True, "query_generation_failed", [], 1)


@pytest.mark.asyncio
async def test_provider_timeout_backstop_cancels_tool_and_skips_selector(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed_timeouts = _record_and_shorten_pipeline_timeouts(monkeypatch)
    captured = _capture_external_outcome(monkeypatch)
    started = asyncio.Event()
    tool = _Tool(started=started, release=asyncio.Event())
    selector_runtime = ScriptedAgentRuntime([])
    runner, _, _ = _runner(
        tasks=[_task("provider timeout")],
        runtime=_runtime(
            query_runtime=ScriptedAgentRuntime([_query_draft(["q"])]),
            selector_runtime=selector_runtime,
            tool=tool,
        ),
    )

    await asyncio.wait_for(_run(runner), timeout=0.5)

    report = captured[0].external_search.task_reports[0]
    assert (
        started.is_set(),
        tool.cancelled,
        selector_runtime.calls,
        report.status,
        report.provider_failed_query_count,
        observed_timeouts.count(15),
    ) == (True, True, [], "provider_failed", 1, 1)


@pytest.mark.asyncio
async def test_selector_timeout_backstop_retries_twice_with_timeout_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed_timeouts = _record_and_shorten_pipeline_timeouts(monkeypatch)
    captured = _capture_external_outcome(monkeypatch)
    selector_runtime = _NeverCompletingRuntime()
    runner, _, _ = _runner(
        tasks=[_task("selector timeout")],
        runtime=_runtime(
            query_runtime=ScriptedAgentRuntime([_query_draft(["q"])]),
            selector_runtime=selector_runtime,
            tool=_Tool({"q": [_candidate("https://example.com/q")]}),
        ),
    )

    await asyncio.wait_for(_run(runner), timeout=0.5)

    report = captured[0].external_search.task_reports[0]
    assert (
        selector_runtime.cancelled,
        selector_runtime.attempt_numbers,
        report.status,
        report.selector_failure_reason,
        observed_timeouts.count(30),
    ) == (True, [1, 2], "selector_failed", "selector_timeout", 3)
