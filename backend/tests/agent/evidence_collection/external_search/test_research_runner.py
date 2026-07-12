"""External search research runner pipeline tests."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import ValidationError

import app.agent.evidence_collection.external_search as external_search_module
from app.agent.planning.contract import ExternalResearchTask


def _as_of() -> datetime:
    return datetime(2026, 7, 4, 9, 0, tzinfo=UTC)


def _task(collection_goal: str) -> ExternalResearchTask:
    return ExternalResearchTask(collection_goal=collection_goal)


def _model(name: str) -> Any:
    return getattr(external_search_module, name)


def _const(name: str) -> int:
    return getattr(external_search_module, name)


def _candidate(
    url: str,
    *,
    title: str | None = None,
    snippet: str | None = "snippet",
    source_name: str | None = "Example",
) -> Any:
    return _model("ExternalSearchCandidate")(
        url=url,
        title=title or url.rsplit("/", maxsplit=1)[-1],
        snippet=snippet,
        source_name=source_name,
    )


def _selection_result(
    selections: list[dict[str, Any]] | None = None,
    *,
    missing: list[str] | None = None,
) -> Any:
    return _model("EvidenceSelectionResult").from_raw(
        selections=selections or [],
        missing=missing or [],
    )


def _request(
    tasks: list[ExternalResearchTask],
    *,
    requested_agent_count: int | None = None,
    effective_agent_count: int = 1,
    target_time_window: str | None = "直近24時間",
) -> Any:
    return external_search_module.ExternalSearchRequest(
        tasks=tasks,
        requested_agent_count=requested_agent_count,
        effective_agent_count=effective_agent_count,
        as_of=_as_of(),
        target_time_window=target_time_window,
    )


def _runner(
    *,
    query_generator: Any,
    search_provider: Any,
    evidence_selector: Any,
    events: Any | None = None,
) -> Any:
    return _model("ExternalSearchResearchRunner")(
        query_generator=query_generator,
        search_provider=search_provider,
        evidence_selector=evidence_selector,
        events=events,
    )


class FakeQueryGenerator:
    def __init__(
        self,
        queries_by_goal: dict[str, list[str]],
        *,
        errors_by_goal: dict[str, Exception] | None = None,
    ) -> None:
        self.queries_by_goal = queries_by_goal
        self.errors_by_goal = errors_by_goal or {}
        self.calls: list[tuple[ExternalResearchTask, datetime, str | None]] = []

    async def generate(
        self,
        *,
        task: ExternalResearchTask,
        as_of: datetime,
        target_time_window: str | None,
    ) -> list[str]:
        self.calls.append((task, as_of, target_time_window))
        if task.collection_goal in self.errors_by_goal:
            raise self.errors_by_goal[task.collection_goal]
        return list(self.queries_by_goal[task.collection_goal])


class ParallelProbeQueryGenerator:
    def __init__(self) -> None:
        self.active = 0
        self.peak = 0
        self.calls: list[tuple[ExternalResearchTask, datetime, str | None]] = []

    async def generate(
        self,
        *,
        task: ExternalResearchTask,
        as_of: datetime,
        target_time_window: str | None,
    ) -> list[str]:
        self.calls.append((task, as_of, target_time_window))
        self.active += 1
        self.peak = max(self.peak, self.active)
        try:
            await asyncio.sleep(0.01)
            return [task.collection_goal]
        finally:
            self.active -= 1


class FakeSearchProvider:
    def __init__(
        self,
        results_by_query: dict[str, list[Any]] | None = None,
        *,
        errors_by_query: dict[str, Exception] | None = None,
    ) -> None:
        self.results_by_query = results_by_query or {}
        self.errors_by_query = errors_by_query or {}
        self.calls: list[tuple[str, int]] = []

    async def search(self, query: str, *, limit: int) -> list[Any]:
        self.calls.append((query, limit))
        if query in self.errors_by_query:
            raise self.errors_by_query[query]
        if query in self.results_by_query:
            return list(self.results_by_query[query])
        return [_candidate(f"https://example.com/{len(self.calls)}")]


class FakeEvidenceSelector:
    def __init__(
        self,
        selections: list[dict[str, Any]] | None = None,
        *,
        missing: list[str] | None = None,
        error: Exception | None = None,
        side_effects: list[Exception | None] | None = None,
    ) -> None:
        self.selections = (
            selections
            if selections is not None
            else [{"candidate_index": 0, "claim": "claim", "why_selected": "why"}]
        )
        self.missing = missing if missing is not None else []
        self.error = error
        self.side_effects = list(side_effects) if side_effects is not None else None
        self.calls: list[tuple[ExternalResearchTask, list[Any], datetime]] = []

    async def select(
        self,
        *,
        task: ExternalResearchTask,
        candidates: list[Any],
        as_of: datetime,
    ) -> Any:
        self.calls.append((task, list(candidates), as_of))
        call_index = len(self.calls) - 1
        if self.side_effects is not None and call_index < len(self.side_effects):
            side_effect = self.side_effects[call_index]
            if side_effect is not None:
                raise side_effect
        if self.error is not None:
            raise self.error
        return _selection_result(self.selections, missing=self.missing)


class FakeEventReporter:
    def __init__(self) -> None:
        self.events: list[Any] = []

    async def event_occurred(self, event: Any) -> None:
        self.events.append(event)


@pytest.mark.asyncio
async def test_runner_limits_parallelism_without_dropping_tasks() -> None:
    tasks = [_task("task-0"), _task("task-1"), _task("task-2")]
    query_generator = ParallelProbeQueryGenerator()
    search_provider = FakeSearchProvider()
    selector = FakeEvidenceSelector()
    runner = _runner(
        query_generator=query_generator,
        search_provider=search_provider,
        evidence_selector=selector,
    )

    result = await runner.search(
        _request(
            tasks,
            requested_agent_count=2,
            effective_agent_count=2,
        )
    )

    assert query_generator.peak == 2
    assert [call[0] for call in query_generator.calls] == tasks
    assert [report.task_index for report in result.task_reports] == [0, 1, 2]
    assert [evidence.task_index for evidence in result.evidence] == [0, 1, 2]


@pytest.mark.asyncio
async def test_runner_clamps_generated_queries_and_never_falls_back_to_goal() -> None:
    task = _task("この目的文を検索 query に流用しない")
    max_chars = _const("EXTERNAL_QUERY_MAX_CHARS")
    queries = [
        "  NVIDIA AI  ",
        "NVIDIA AI",
        "",
        "x" * (max_chars + 25),
        "Blackwell supply chain",
        "fourth query is beyond the limit",
    ]
    query_generator = FakeQueryGenerator({task.collection_goal: queries})
    search_provider = FakeSearchProvider()
    selector = FakeEvidenceSelector(selections=[])
    runner = _runner(
        query_generator=query_generator,
        search_provider=search_provider,
        evidence_selector=selector,
    )

    result = await runner.search(_request([task]))

    expected_queries = [
        "NVIDIA AI",
        "x" * max_chars,
        "Blackwell supply chain",
    ]
    assert [query for query, _ in search_provider.calls] == expected_queries
    assert result.task_reports[0].generated_queries == expected_queries
    assert task.collection_goal not in result.task_reports[0].generated_queries


@pytest.mark.asyncio
async def test_runner_partial_provider_failure_continues_with_visible_count() -> None:
    task = _task("provider partial failure")
    provider_error = _model("ExternalSearchProviderError")("provider failed")
    query_generator = FakeQueryGenerator({task.collection_goal: ["q1", "q2", "q3"]})
    search_provider = FakeSearchProvider(
        {
            "q1": [_candidate("https://example.com/q1")],
            "q3": [_candidate("https://example.com/q3")],
        },
        errors_by_query={"q2": provider_error},
    )
    selector = FakeEvidenceSelector()
    runner = _runner(
        query_generator=query_generator,
        search_provider=search_provider,
        evidence_selector=selector,
    )

    result = await runner.search(_request([task]))

    report = result.task_reports[0]
    assert report.status == "succeeded"
    assert report.provider_failed_query_count == 1
    assert report.candidate_count == 2
    assert report.evidence_count == 1


@pytest.mark.asyncio
async def test_runner_reports_live_events_for_successful_task() -> None:
    task = _task("external live events")
    reporter = FakeEventReporter()
    query_generator = FakeQueryGenerator({task.collection_goal: [" q1 ", "q2"]})
    search_provider = FakeSearchProvider(
        {
            "q1": [_candidate("https://example.com/q1")],
            "q2": [_candidate("https://example.com/q2")],
        }
    )
    selector = FakeEvidenceSelector(
        selections=[
            {"candidate_index": 0, "claim": "claim", "why_selected": "why"},
            {"candidate_index": 1, "claim": "claim 2", "why_selected": "why"},
        ]
    )
    runner = _runner(
        query_generator=query_generator,
        search_provider=search_provider,
        evidence_selector=selector,
        events=reporter,
    )

    result = await runner.search(_request([task]))

    assert result.task_reports[0].status == "succeeded"
    assert [event.type for event in reporter.events] == [
        "external_search.queries_generated",
        "external_search.candidates_fetched",
        "external_search.evidence_selected",
    ]
    assert reporter.events[0].task_index == 0
    assert reporter.events[0].queries == ["q1", "q2"]
    assert reporter.events[1].candidate_count == 2
    assert reporter.events[2].evidence_count == 2


@pytest.mark.asyncio
async def test_runner_omits_later_events_when_all_provider_calls_fail() -> None:
    task = _task("provider all failure")
    reporter = FakeEventReporter()
    provider_error = _model("ExternalSearchProviderError")("provider failed")
    query_generator = FakeQueryGenerator({task.collection_goal: ["q1", "q2"]})
    runner = _runner(
        query_generator=query_generator,
        search_provider=FakeSearchProvider(
            errors_by_query={"q1": provider_error, "q2": provider_error},
        ),
        evidence_selector=FakeEvidenceSelector(),
        events=reporter,
    )

    result = await runner.search(_request([task]))

    assert result.task_reports[0].status == "provider_failed"
    assert [event.type for event in reporter.events] == [
        "external_search.queries_generated"
    ]


@pytest.mark.asyncio
async def test_runner_all_provider_failures_are_classified_without_selection() -> None:
    task = _task("provider all failure")
    provider_error = _model("ExternalSearchProviderError")("provider failed")
    query_generator = FakeQueryGenerator({task.collection_goal: ["q1", "q2"]})
    search_provider = FakeSearchProvider(
        errors_by_query={"q1": provider_error, "q2": provider_error},
    )
    selector = FakeEvidenceSelector()
    runner = _runner(
        query_generator=query_generator,
        search_provider=search_provider,
        evidence_selector=selector,
    )

    result = await runner.search(_request([task]))

    assert result.evidence == []
    assert selector.calls == []
    assert result.task_reports[0].status == "provider_failed"
    assert result.task_reports[0].provider_failed_query_count == 2


@pytest.mark.parametrize(
    ("stage", "expected_status"),
    [
        ("query", "query_generation_failed"),
        ("provider", "provider_failed"),
        ("selector", "selector_failed"),
    ],
)
@pytest.mark.asyncio
async def test_runner_timeouts_map_to_stage_failure_status(
    stage: str,
    expected_status: str,
) -> None:
    task = _task(f"{stage} timeout")
    query_generator = FakeQueryGenerator(
        {task.collection_goal: ["q"]},
        errors_by_goal={task.collection_goal: TimeoutError()}
        if stage == "query"
        else None,
    )
    search_provider = FakeSearchProvider(
        {"q": [_candidate("https://example.com/q")]},
        errors_by_query={"q": TimeoutError()} if stage == "provider" else None,
    )
    selector = FakeEvidenceSelector(
        error=TimeoutError() if stage == "selector" else None,
    )
    runner = _runner(
        query_generator=query_generator,
        search_provider=search_provider,
        evidence_selector=selector,
    )

    result = await runner.search(_request([task]))

    assert result.evidence == []
    assert result.task_reports[0].status == expected_status


@pytest.mark.asyncio
async def test_runner_retries_selector_once_with_same_inputs() -> None:
    task = _task("selector retry")
    selector_error = _model("ExternalEvidenceSelectorError")(
        reason="external_search_deepseek_arguments_schema_invalid"
    )
    query_generator = FakeQueryGenerator({task.collection_goal: ["q"]})
    search_provider = FakeSearchProvider({"q": [_candidate("https://example.com/q")]})
    selector = FakeEvidenceSelector(side_effects=[selector_error, None])
    runner = _runner(
        query_generator=query_generator,
        search_provider=search_provider,
        evidence_selector=selector,
    )

    result = await runner.search(_request([task]))

    assert len(selector.calls) == 2
    assert selector.calls[0] == selector.calls[1]
    assert query_generator.calls == [(task, _as_of(), "直近24時間")]
    assert search_provider.calls == [
        ("q", _const("EXTERNAL_SEARCH_CANDIDATES_PER_QUERY"))
    ]
    report = result.task_reports[0]
    assert report.status == "succeeded"
    assert report.evidence_count == 1
    assert report.selector_failure_reason is None
    assert [evidence.source_ref for evidence in result.evidence] == ["external-0-0"]


@pytest.mark.asyncio
async def test_runner_selector_failure_after_retry_reports_reason() -> None:
    task = _task("selector failure")
    selector_error = _model("ExternalEvidenceSelectorError")(
        reason="external_search_deepseek_arguments_schema_invalid"
    )
    query_generator = FakeQueryGenerator({task.collection_goal: ["q"]})
    search_provider = FakeSearchProvider({"q": [_candidate("https://example.com/q")]})
    selector = FakeEvidenceSelector(
        side_effects=[selector_error, selector_error],
    )
    runner = _runner(
        query_generator=query_generator,
        search_provider=search_provider,
        evidence_selector=selector,
    )

    result = await runner.search(_request([task]))

    report = result.task_reports[0]
    assert result.evidence == []
    assert len(selector.calls) == 2
    assert report.status == "selector_failed"
    assert report.selector_failure_reason == (
        "external_search_deepseek_arguments_schema_invalid"
    )


@pytest.mark.asyncio
async def test_runner_selector_timeout_after_retry_reports_timeout_reason() -> None:
    task = _task("selector timeout")
    query_generator = FakeQueryGenerator({task.collection_goal: ["q"]})
    search_provider = FakeSearchProvider({"q": [_candidate("https://example.com/q")]})
    selector = FakeEvidenceSelector(
        side_effects=[TimeoutError(), TimeoutError()],
    )
    runner = _runner(
        query_generator=query_generator,
        search_provider=search_provider,
        evidence_selector=selector,
    )

    result = await runner.search(_request([task]))

    report = result.task_reports[0]
    assert result.evidence == []
    assert len(selector.calls) == 2
    assert report.status == "selector_failed"
    assert report.selector_failure_reason == "selector_timeout"


@pytest.mark.asyncio
async def test_runner_keeps_other_task_evidence_when_one_task_fails() -> None:
    failed = _task("failed query generation")
    succeeded = _task("successful task")
    query_error = _model("ExternalQueryGenerationError")("query failed")
    query_generator = FakeQueryGenerator(
        {
            failed.collection_goal: ["unused"],
            succeeded.collection_goal: ["ok"],
        },
        errors_by_goal={failed.collection_goal: query_error},
    )
    search_provider = FakeSearchProvider(
        {"ok": [_candidate("https://example.com/ok")]},
    )
    selector = FakeEvidenceSelector()
    runner = _runner(
        query_generator=query_generator,
        search_provider=search_provider,
        evidence_selector=selector,
    )

    result = await runner.search(_request([failed, succeeded], effective_agent_count=2))

    assert [report.status for report in result.task_reports] == [
        "query_generation_failed",
        "succeeded",
    ]
    assert [evidence.task_index for evidence in result.evidence] == [1]


@pytest.mark.asyncio
async def test_runner_unclassified_exceptions_propagate() -> None:
    task = _task("bug")
    query_generator = FakeQueryGenerator(
        {task.collection_goal: ["unused"]},
        errors_by_goal={task.collection_goal: ValueError("unexpected bug")},
    )
    runner = _runner(
        query_generator=query_generator,
        search_provider=FakeSearchProvider(),
        evidence_selector=FakeEvidenceSelector(),
    )

    with pytest.raises(ValueError, match="unexpected bug"):
        await runner.search(_request([task]))


@pytest.mark.asyncio
async def test_runner_dedupes_and_round_robins_candidate_pool_for_selector() -> None:
    task = _task("pool")
    pool_limit = _const("EXTERNAL_SEARCH_CANDIDATE_POOL_LIMIT_PER_TASK")
    query_generator = FakeQueryGenerator({task.collection_goal: ["q1", "q2"]})
    search_provider = FakeSearchProvider(
        {
            "q1": [
                _candidate("https://example.com/q1-0"),
                _candidate("https://example.com/q1-1"),
                _candidate("https://example.com/shared"),
            ],
            "q2": [
                _candidate("https://example.com/q2-0"),
                _candidate("https://example.com/q2-1"),
                _candidate("https://example.com/shared"),
            ],
        }
    )
    selector = FakeEvidenceSelector(selections=[])
    runner = _runner(
        query_generator=query_generator,
        search_provider=search_provider,
        evidence_selector=selector,
    )

    await runner.search(_request([task]))

    assert {limit for _, limit in search_provider.calls} == {
        _const("EXTERNAL_SEARCH_CANDIDATES_PER_QUERY"),
    }
    candidate_urls = [
        str(candidate.url)
        for _, candidates, _ in selector.calls
        for candidate in candidates
    ]
    assert candidate_urls[:4] == [
        "https://example.com/q1-0",
        "https://example.com/q2-0",
        "https://example.com/q1-1",
        "https://example.com/q2-1",
    ]
    assert candidate_urls.count("https://example.com/shared") == 1
    assert len(candidate_urls) <= pool_limit


@pytest.mark.asyncio
async def test_runner_truncates_provider_results_over_requested_limit() -> None:
    task = _task("provider over limit")
    candidate_limit = _const("EXTERNAL_SEARCH_CANDIDATES_PER_QUERY")
    query_generator = FakeQueryGenerator({task.collection_goal: ["q"]})
    search_provider = FakeSearchProvider(
        {
            "q": [
                _candidate(f"https://example.com/candidate-{index}")
                for index in range(candidate_limit + 3)
            ]
        }
    )
    selector = FakeEvidenceSelector(selections=[])
    runner = _runner(
        query_generator=query_generator,
        search_provider=search_provider,
        evidence_selector=selector,
    )

    result = await runner.search(_request([task]))

    candidates = selector.calls[0][1]
    assert len(candidates) == candidate_limit
    assert str(candidates[-1].url) == (
        f"https://example.com/candidate-{candidate_limit - 1}"
    )
    assert result.task_reports[0].candidate_count == candidate_limit


@pytest.mark.asyncio
async def test_runner_caps_large_candidate_pool_after_round_robin() -> None:
    task = _task("pool limit")
    pool_limit = _const("EXTERNAL_SEARCH_CANDIDATE_POOL_LIMIT_PER_TASK")
    query_generator = FakeQueryGenerator({task.collection_goal: ["q1", "q2", "q3"]})
    search_provider = FakeSearchProvider(
        {
            query: [
                _candidate(f"https://example.com/{query}-{index}")
                for index in range(pool_limit)
            ]
            for query in ("q1", "q2", "q3")
        }
    )
    selector = FakeEvidenceSelector(selections=[])
    runner = _runner(
        query_generator=query_generator,
        search_provider=search_provider,
        evidence_selector=selector,
    )

    result = await runner.search(_request([task]))

    candidates = selector.calls[0][1]
    assert len(candidates) == pool_limit
    assert result.task_reports[0].candidate_count == pool_limit
    assert [str(candidate.url) for candidate in candidates[:6]] == [
        "https://example.com/q1-0",
        "https://example.com/q2-0",
        "https://example.com/q3-0",
        "https://example.com/q1-1",
        "https://example.com/q2-1",
        "https://example.com/q3-1",
    ]


@pytest.mark.asyncio
async def test_runner_empty_provider_results_are_successful_zero_evidence() -> None:
    task = _task("empty provider results")
    query_generator = FakeQueryGenerator({task.collection_goal: ["q1", "q2"]})
    search_provider = FakeSearchProvider({"q1": [], "q2": []})
    selector = FakeEvidenceSelector(selections=[])
    runner = _runner(
        query_generator=query_generator,
        search_provider=search_provider,
        evidence_selector=selector,
    )

    result = await runner.search(_request([task]))

    assert result.evidence == []
    assert selector.calls == []
    assert result.task_reports[0].status == "succeeded"
    assert result.task_reports[0].provider_failed_query_count == 0
    assert result.task_reports[0].candidate_count == 0
    assert result.task_reports[0].evidence_count == 0


@pytest.mark.asyncio
async def test_runner_selection_validation_drops_bad_indexes_and_caps_evidence() -> (
    None
):
    task = _task("selection validation")
    evidence_limit = _const("EXTERNAL_SEARCH_EVIDENCE_LIMIT_PER_TASK")
    query_generator = FakeQueryGenerator({task.collection_goal: ["q"]})
    search_provider = FakeSearchProvider(
        {
            "q": [
                _candidate(f"https://example.com/candidate-{index}")
                for index in range(evidence_limit + 2)
            ]
        }
    )
    selections = [
        {"candidate_index": 0, "claim": "zero", "why_selected": "why"},
        {"candidate_index": 99, "claim": "bad", "why_selected": "why"},
        {"candidate_index": 0, "claim": "duplicate", "why_selected": "why"},
    ] + [
        {"candidate_index": index, "claim": f"claim {index}", "why_selected": "why"}
        for index in range(1, evidence_limit + 1)
    ]
    selector = FakeEvidenceSelector(selections=selections)
    runner = _runner(
        query_generator=query_generator,
        search_provider=search_provider,
        evidence_selector=selector,
    )

    result = await runner.search(_request([task]))

    report = result.task_reports[0]
    assert report.status == "succeeded"
    assert report.evidence_count == evidence_limit
    assert report.dropped_selection_count == 3
    assert [evidence.source_ref for evidence in result.evidence] == [
        f"external-0-{index}" for index in range(evidence_limit)
    ]


@pytest.mark.asyncio
async def test_runner_source_ref_uses_candidate_index_not_selection_order() -> None:
    task = _task("source ref")
    query_generator = FakeQueryGenerator({task.collection_goal: ["q"]})
    search_provider = FakeSearchProvider(
        {
            "q": [
                _candidate("https://example.com/zero"),
                _candidate("https://example.com/one"),
                _candidate("https://example.com/two"),
            ]
        }
    )
    selector = FakeEvidenceSelector(
        selections=[
            {"candidate_index": 2, "claim": "two", "why_selected": "why"},
            {"candidate_index": 0, "claim": "zero", "why_selected": "why"},
        ],
    )
    runner = _runner(
        query_generator=query_generator,
        search_provider=search_provider,
        evidence_selector=selector,
    )

    result = await runner.search(_request([task]))

    assert [evidence.source_ref for evidence in result.evidence] == [
        "external-0-2",
        "external-0-0",
    ]


@pytest.mark.asyncio
async def test_runner_empty_selection_is_successful_zero_evidence() -> None:
    task = _task("empty selection")
    query_generator = FakeQueryGenerator({task.collection_goal: ["q"]})
    search_provider = FakeSearchProvider({"q": [_candidate("https://example.com/q")]})
    selector = FakeEvidenceSelector(selections=[])
    runner = _runner(
        query_generator=query_generator,
        search_provider=search_provider,
        evidence_selector=selector,
    )

    result = await runner.search(_request([task]))

    assert result.evidence == []
    assert result.task_reports[0].status == "succeeded"
    assert result.task_reports[0].evidence_count == 0


def test_selection_result_factory_clamps_strings_and_missing_items() -> None:
    claim_limit = _const("EVIDENCE_CLAIM_MAX_CHARS")
    why_limit = _const("EVIDENCE_WHY_SELECTED_MAX_CHARS")
    missing_item_limit = _const("MISSING_ITEM_MAX_CHARS")
    missing_count_limit = _const("EXTERNAL_SEARCH_MISSING_LIMIT_PER_TASK")

    result = _selection_result(
        [
            {
                "candidate_index": 0,
                "claim": "c" * (claim_limit + 20),
                "why_selected": "w" * (why_limit + 20),
            }
        ],
        missing=[
            f"{index}-" + "m" * (missing_item_limit + 20)
            for index in range(missing_count_limit + 2)
        ],
    )

    assert len(result.selections) == 1
    assert len(result.selections[0].claim) == claim_limit
    assert len(result.selections[0].why_selected) == why_limit
    assert len(result.missing) == missing_count_limit
    assert all(len(item) <= missing_item_limit for item in result.missing)


def test_direct_models_reject_over_cap_selector_strings() -> None:
    claim_limit = _const("EVIDENCE_CLAIM_MAX_CHARS")
    why_limit = _const("EVIDENCE_WHY_SELECTED_MAX_CHARS")
    missing_item_limit = _const("MISSING_ITEM_MAX_CHARS")

    with pytest.raises(ValidationError):
        _model("EvidenceSelection")(
            candidate_index=0,
            claim="c" * (claim_limit + 1),
            why_selected="why",
        )

    with pytest.raises(ValidationError):
        _model("EvidenceSelection")(
            candidate_index=0,
            claim="claim",
            why_selected="w" * (why_limit + 1),
        )

    with pytest.raises(ValidationError):
        _model("EvidenceSelectionResult")(
            selections=[],
            missing=["m" * (missing_item_limit + 1)],
        )


@pytest.mark.asyncio
async def test_runner_passes_as_of_and_target_time_window_to_ports() -> None:
    task = _task("time propagation")
    query_generator = FakeQueryGenerator({task.collection_goal: ["q"]})
    search_provider = FakeSearchProvider({"q": [_candidate("https://example.com/q")]})
    selector = FakeEvidenceSelector(selections=[])
    runner = _runner(
        query_generator=query_generator,
        search_provider=search_provider,
        evidence_selector=selector,
    )

    await runner.search(_request([task], target_time_window="今日"))

    assert query_generator.calls == [(task, _as_of(), "今日")]
    assert selector.calls[0][0] == task
    assert selector.calls[0][2] == _as_of()
