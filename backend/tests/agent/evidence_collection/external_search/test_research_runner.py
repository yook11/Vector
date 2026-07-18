"""ExternalSearchResearchRunner のAgent / Runtime workflow 契約。"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib import import_module
from types import ModuleType
from typing import Any

import pytest
from pydantic import ValidationError

from app.agent.agent import Agent
from app.agent.planning.contract import ExternalResearchTask
from app.agent.runtime.contract import AgentResponseDefect, AgentResponseInvalidError
from app.analysis.ai_provider_errors import AIProviderNetworkError
from app.analysis.deepseek_error_translator import DeepSeekStateReason


def _required_module(module_name: str) -> ModuleType:
    try:
        return import_module(module_name)
    except ModuleNotFoundError as exc:
        pytest.fail(f"PR2 workflow module is missing: {module_name} ({exc.name})")


def _required_attribute(module: ModuleType, name: str) -> Any:
    if not hasattr(module, name):
        pytest.fail(f"PR2 workflow contract is missing: {module.__name__}.{name}")
    return getattr(module, name)


def _contracts() -> ModuleType:
    return _required_module("app.agent.evidence_collection.external_search.contract")


def _agents() -> ModuleType:
    return _required_module("app.agent.evidence_collection.external_search.agent")


def _runner_type() -> type[Any]:
    return _required_attribute(
        _required_module("app.agent.evidence_collection.external_search.runner"),
        "ExternalSearchResearchRunner",
    )


def _query_agent() -> Agent[Any, Any]:
    return _required_attribute(_agents(), "EXTERNAL_QUERY_AGENT")


def _selector_agent() -> Agent[Any, Any]:
    return _required_attribute(_agents(), "EXTERNAL_EVIDENCE_SELECTOR_AGENT")


def _as_of() -> datetime:
    return datetime(2026, 7, 19, 9, 0, tzinfo=UTC)


def _task(goal: str) -> ExternalResearchTask:
    return ExternalResearchTask(collection_goal=goal)


def _request(
    tasks: list[ExternalResearchTask],
    *,
    effective_agent_count: int = 1,
    target_time_window: str | None = "直近24時間",
) -> Any:
    return _required_attribute(_contracts(), "ExternalSearchRequest")(
        tasks=tasks,
        effective_agent_count=effective_agent_count,
        as_of=_as_of(),
        target_time_window=target_time_window,
    )


def _candidate(url: str, *, title: str | None = None) -> Any:
    return _required_attribute(_contracts(), "ExternalSearchCandidate")(
        url=url,
        title=title or url.rsplit("/", maxsplit=1)[-1],
        snippet="snippet",
        source_name="Example",
        published_at=_as_of(),
    )


def _query_draft(queries: object) -> Any:
    return _required_attribute(_contracts(), "ExternalQueryDraft").model_validate(
        {"queries": queries}
    )


def _selection_draft(
    selections: list[dict[str, Any]] | None = None,
    *,
    missing: list[str] | None = None,
) -> Any:
    return _required_attribute(
        _contracts(), "ExternalEvidenceSelectionDraft"
    ).model_validate({"selections": selections or [], "missing": missing or []})


@dataclass(frozen=True, slots=True)
class RuntimeCall:
    agent: Agent[Any, Any]
    input: object
    attempt_number: int


class FakeRuntime:
    """1 attempt outcomeだけを返すRuntime fake。retry policyは持たない。"""

    def __init__(self, outcomes: Sequence[object | BaseException]) -> None:
        self._outcomes = list(outcomes)
        self.calls: list[RuntimeCall] = []

    async def invoke[InputT, OutputT](
        self,
        agent: Agent[InputT, OutputT],
        input: InputT,
        *,
        attempt_number: int,
    ) -> OutputT:
        self.calls.append(
            RuntimeCall(agent=agent, input=input, attempt_number=attempt_number)
        )
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome  # type: ignore[return-value]


class ParallelQueryRuntime:
    def __init__(self) -> None:
        self.active = 0
        self.peak = 0
        self.calls: list[RuntimeCall] = []

    async def invoke[InputT, OutputT](
        self,
        agent: Agent[InputT, OutputT],
        input: InputT,
        *,
        attempt_number: int,
    ) -> OutputT:
        self.calls.append(RuntimeCall(agent, input, attempt_number))
        self.active += 1
        self.peak = max(self.peak, self.active)
        try:
            await asyncio.sleep(0.01)
            return _query_draft([input.task.collection_goal])  # type: ignore[return-value]
        finally:
            self.active -= 1


class FakeSearchProvider:
    def __init__(
        self,
        results_by_query: dict[str, list[Any]] | None = None,
        *,
        errors_by_query: dict[str, BaseException] | None = None,
    ) -> None:
        self._results_by_query = results_by_query or {}
        self._errors_by_query = errors_by_query or {}
        self.calls: list[tuple[str, int]] = []

    async def search(self, query: str, *, limit: int) -> list[Any]:
        self.calls.append((query, limit))
        if query in self._errors_by_query:
            raise self._errors_by_query[query]
        return list(self._results_by_query.get(query, []))


class FakeEventReporter:
    def __init__(self) -> None:
        self.events: list[Any] = []

    async def event_occurred(self, event: Any) -> None:
        self.events.append(event)


def _runner(
    *,
    query_runtime: FakeRuntime,
    search_provider: FakeSearchProvider,
    selector_runtime: FakeRuntime,
    events: FakeEventReporter | None = None,
) -> Any:
    return _runner_type()(
        query_agent=_query_agent(),
        query_runtime=query_runtime,
        search_provider=search_provider,
        selector_agent=_selector_agent(),
        selector_runtime=selector_runtime,
        events=events,
    )


@pytest.mark.asyncio
async def test_query_normalization_and_selector_projection() -> None:
    task = _task("NVIDIA の新製品を確認する")
    max_chars = _required_attribute(_contracts(), "EXTERNAL_QUERY_MAX_CHARS")
    query_runtime = FakeRuntime(
        [
            _query_draft(
                [
                    "  NVIDIA AI  ",
                    "NVIDIA AI",
                    "",
                    "x" * (max_chars + 10),
                    "Blackwell supply chain",
                    "beyond query cap",
                ]
            )
        ]
    )
    selector_runtime = FakeRuntime([_selection_draft([])])
    search_provider = FakeSearchProvider(
        {
            "NVIDIA AI": [_candidate("https://example.com/one")],
            "x" * max_chars: [_candidate("https://example.com/two")],
            "Blackwell supply chain": [_candidate("https://example.com/three")],
        }
    )
    runner = _runner(
        query_runtime=query_runtime,
        search_provider=search_provider,
        selector_runtime=selector_runtime,
    )

    result = await runner.search(_request([task]))

    assert [query for query, _ in search_provider.calls] == [
        "NVIDIA AI",
        "x" * max_chars,
        "Blackwell supply chain",
    ]
    assert query_runtime.calls == [
        RuntimeCall(
            agent=_query_agent(),
            input=_required_attribute(_contracts(), "ExternalQueryGenerationInput")(
                task=task,
                as_of=_as_of(),
                target_time_window="直近24時間",
            ),
            attempt_number=1,
        )
    ]
    selector_input = selector_runtime.calls[0].input
    assert selector_runtime.calls[0].agent is _selector_agent()
    assert selector_runtime.calls[0].attempt_number == 1
    assert all(not hasattr(candidate, "url") for candidate in selector_input.candidates)
    assert result.task_reports[0].generated_queries == [
        "NVIDIA AI",
        "x" * max_chars,
        "Blackwell supply chain",
    ]


@pytest.mark.asyncio
async def test_query_classified_failure_short_circuits_search_and_selector() -> None:
    task = _task("query failure")
    query_runtime = FakeRuntime(
        [AgentResponseInvalidError(AgentResponseDefect.RESPONSE_NOT_JSON)]
    )
    selector_runtime = FakeRuntime([_selection_draft([])])
    search_provider = FakeSearchProvider()

    result = await _runner(
        query_runtime=query_runtime,
        search_provider=search_provider,
        selector_runtime=selector_runtime,
    ).search(_request([task]))

    assert len(query_runtime.calls) == 1
    assert search_provider.calls == []
    assert selector_runtime.calls == []
    assert result.task_reports[0].status == "query_generation_failed"


@pytest.mark.asyncio
async def test_query_provider_failure_and_timeout_short_circuit_once() -> None:
    task = _task("query failure kinds")
    outcomes = [
        AIProviderNetworkError(reason=DeepSeekStateReason.TIMEOUT),
        TimeoutError(),
    ]

    for outcome in outcomes:
        query_runtime = FakeRuntime([outcome])
        selector_runtime = FakeRuntime([_selection_draft([])])
        search_provider = FakeSearchProvider()
        result = await _runner(
            query_runtime=query_runtime,
            search_provider=search_provider,
            selector_runtime=selector_runtime,
        ).search(_request([task]))

        assert len(query_runtime.calls) == 1
        assert search_provider.calls == []
        assert selector_runtime.calls == []
        assert result.task_reports[0].status == "query_generation_failed"


@pytest.mark.asyncio
async def test_query_unclassified_exception_propagates_from_first_attempt() -> None:
    error = RuntimeError("query unclassified")
    query_runtime = FakeRuntime([error])

    with pytest.raises(RuntimeError) as raised:
        await _runner(
            query_runtime=query_runtime,
            search_provider=FakeSearchProvider(),
            selector_runtime=FakeRuntime([_selection_draft([])]),
        ).search(_request([_task("unclassified")]))

    assert raised.value is error
    assert len(query_runtime.calls) == 1


@pytest.mark.asyncio
async def test_selector_retries_once_with_same_typed_input_instance() -> None:
    task = _task("selector retry")
    query_runtime = FakeRuntime([_query_draft(["q"])])
    selector_runtime = FakeRuntime(
        [
            AgentResponseInvalidError(AgentResponseDefect.OUTPUT_SCHEMA_MISMATCH),
            _selection_draft(
                [{"candidate_index": 0, "claim": "claim", "why_selected": "why"}]
            ),
        ]
    )
    runner = _runner(
        query_runtime=query_runtime,
        search_provider=FakeSearchProvider(
            {"q": [_candidate("https://example.com/q")]}
        ),
        selector_runtime=selector_runtime,
    )

    result = await runner.search(_request([task]))

    assert [call.attempt_number for call in selector_runtime.calls] == [1, 2]
    assert selector_runtime.calls[0].input is selector_runtime.calls[1].input
    assert result.task_reports[0].status == "succeeded"
    assert [item.source_ref for item in result.evidence] == ["external-0-0"]


@pytest.mark.asyncio
async def test_selector_finalization_invalid_draft_is_schema_mismatch_and_retries() -> (
    None
):
    selector_runtime = FakeRuntime(
        [
            _selection_draft(
                [{"candidate_index": 0, "claim": "", "why_selected": "why"}]
            ),
            _selection_draft(
                [{"candidate_index": 0, "claim": "claim", "why_selected": "why"}]
            ),
        ]
    )

    result = await _runner(
        query_runtime=FakeRuntime([_query_draft(["q"])]),
        search_provider=FakeSearchProvider(
            {"q": [_candidate("https://example.com/q")]}
        ),
        selector_runtime=selector_runtime,
    ).search(_request([_task("finalization retry")]))

    assert [call.attempt_number for call in selector_runtime.calls] == [1, 2]
    assert result.task_reports[0].status == "succeeded"


@pytest.mark.asyncio
async def test_selector_retries_only_classified_provider_or_timeout_failures() -> None:
    task = _task("selector failures")
    retryable = [
        (
            AgentResponseInvalidError(AgentResponseDefect.RESPONSE_NOT_JSON),
            AgentResponseDefect.RESPONSE_NOT_JSON.value,
        ),
        (AIProviderNetworkError(reason=DeepSeekStateReason.TIMEOUT), "timeout"),
        (TimeoutError(), "selector_timeout"),
    ]

    for error, expected_reason in retryable:
        selector_runtime = FakeRuntime([error, error])
        result = await _runner(
            query_runtime=FakeRuntime([_query_draft(["q"])]),
            search_provider=FakeSearchProvider(
                {"q": [_candidate("https://example.com/q")]}
            ),
            selector_runtime=selector_runtime,
        ).search(_request([task]))

        assert [call.attempt_number for call in selector_runtime.calls] == [1, 2]
        assert result.task_reports[0].status == "selector_failed"
        assert result.task_reports[0].selector_failure_reason == expected_reason


@pytest.mark.asyncio
async def test_selector_unclassified_exception_does_not_retry() -> None:
    error = RuntimeError("selector unclassified")
    selector_runtime = FakeRuntime([error])

    with pytest.raises(RuntimeError) as raised:
        await _runner(
            query_runtime=FakeRuntime([_query_draft(["q"])]),
            search_provider=FakeSearchProvider(
                {"q": [_candidate("https://example.com/q")]}
            ),
            selector_runtime=selector_runtime,
        ).search(_request([_task("selector unclassified")]))

    assert raised.value is error
    assert len(selector_runtime.calls) == 1


@pytest.mark.asyncio
async def test_selector_finalization_drops_indexes_and_restores_sources() -> None:
    task = _task("selection finalization")
    contracts = _contracts()
    claim_limit = _required_attribute(contracts, "EVIDENCE_CLAIM_MAX_CHARS")
    why_limit = _required_attribute(contracts, "EVIDENCE_WHY_SELECTED_MAX_CHARS")
    selector_runtime = FakeRuntime(
        [
            _selection_draft(
                [
                    {
                        "candidate_index": 1,
                        "claim": "c" * (claim_limit + 10),
                        "why_selected": "w" * (why_limit + 10),
                    },
                    {"candidate_index": 1, "claim": "duplicate", "why_selected": "why"},
                    {"candidate_index": 99, "claim": "out", "why_selected": "why"},
                    {"candidate_index": 0, "claim": "first", "why_selected": "why"},
                ],
                missing=["m" * 400],
            )
        ]
    )
    second = _candidate("https://example.com/second", title="second title")
    result = await _runner(
        query_runtime=FakeRuntime([_query_draft(["q"])]),
        search_provider=FakeSearchProvider(
            {
                "q": [
                    _candidate("https://example.com/first", title="first title"),
                    second,
                ]
            }
        ),
        selector_runtime=selector_runtime,
    ).search(_request([task]))

    assert [item.source_ref for item in result.evidence] == [
        "external-0-1",
        "external-0-0",
    ]
    assert result.evidence[0].url == second.url
    assert result.evidence[0].title == "second title"
    assert len(result.evidence[0].claim) == claim_limit
    assert len(result.evidence[0].why_selected) == why_limit
    assert result.task_reports[0].dropped_selection_count == 2
    assert len(result.task_reports[0].missing[0]) == _required_attribute(
        contracts, "MISSING_ITEM_MAX_CHARS"
    )


@pytest.mark.asyncio
async def test_empty_pool_skips_selector_and_succeeds() -> None:
    selector_runtime = FakeRuntime([_selection_draft([])])
    result = await _runner(
        query_runtime=FakeRuntime([_query_draft(["q"])]),
        search_provider=FakeSearchProvider({"q": []}),
        selector_runtime=selector_runtime,
    ).search(_request([_task("empty pool")]))

    assert selector_runtime.calls == []
    assert result.evidence == []
    assert result.task_reports[0].status == "succeeded"
    assert result.task_reports[0].candidate_count == 0


@pytest.mark.asyncio
async def test_task_order_events_and_partial_provider_failure() -> None:
    tasks = [_task("first"), _task("second")]
    reporter = FakeEventReporter()
    provider_error = _required_attribute(_contracts(), "ExternalSearchProviderError")(
        "provider failure"
    )
    query_runtime = FakeRuntime([_query_draft(["q1"]), _query_draft(["q2", "q3"])])
    selector_runtime = FakeRuntime([_selection_draft([]), _selection_draft([])])
    result = await _runner(
        query_runtime=query_runtime,
        search_provider=FakeSearchProvider(
            {
                "q1": [_candidate("https://example.com/q1")],
                "q2": [_candidate("https://example.com/q2")],
            },
            errors_by_query={"q3": provider_error},
        ),
        selector_runtime=selector_runtime,
        events=reporter,
    ).search(_request(tasks, effective_agent_count=2))

    assert [report.task_index for report in result.task_reports] == [0, 1]
    assert [report.provider_failed_query_count for report in result.task_reports] == [
        0,
        1,
    ]
    assert sorted(event.task_index for event in reporter.events) == [0, 0, 0, 1, 1, 1]


@pytest.mark.asyncio
async def test_task_parallelism_keeps_all_reports() -> None:
    tasks = [_task("task-0"), _task("task-1"), _task("task-2")]
    query_runtime = ParallelQueryRuntime()
    selector_runtime = FakeRuntime([_selection_draft([]) for _ in tasks])
    result = await _runner(
        query_runtime=query_runtime,
        search_provider=FakeSearchProvider(
            {
                task.collection_goal: [
                    _candidate(f"https://example.com/{task.collection_goal}")
                ]
                for task in tasks
            }
        ),
        selector_runtime=selector_runtime,
    ).search(_request(tasks, effective_agent_count=2))

    assert query_runtime.peak == 2
    assert [call.input.task for call in query_runtime.calls] == tasks
    assert [report.task_index for report in result.task_reports] == [0, 1, 2]
    assert result.evidence == []


@pytest.mark.asyncio
async def test_all_provider_failures_skip_selector_and_later_progress_events() -> None:
    reporter = FakeEventReporter()
    provider_error = _required_attribute(_contracts(), "ExternalSearchProviderError")(
        "provider failure"
    )
    selector_runtime = FakeRuntime([_selection_draft([])])
    result = await _runner(
        query_runtime=FakeRuntime([_query_draft(["q1", "q2"])]),
        search_provider=FakeSearchProvider(
            errors_by_query={"q1": provider_error, "q2": provider_error}
        ),
        selector_runtime=selector_runtime,
        events=reporter,
    ).search(_request([_task("all provider failures")]))

    assert result.evidence == []
    assert selector_runtime.calls == []
    assert result.task_reports[0].status == "provider_failed"
    assert result.task_reports[0].provider_failed_query_count == 2
    assert [event.type for event in reporter.events] == [
        "external_search.queries_generated"
    ]


@pytest.mark.asyncio
async def test_provider_timeout_maps_to_provider_failure_without_selector() -> None:
    selector_runtime = FakeRuntime([_selection_draft([])])
    result = await _runner(
        query_runtime=FakeRuntime([_query_draft(["q"])]),
        search_provider=FakeSearchProvider(errors_by_query={"q": TimeoutError()}),
        selector_runtime=selector_runtime,
    ).search(_request([_task("provider timeout")]))

    assert result.task_reports[0].status == "provider_failed"
    assert selector_runtime.calls == []


@pytest.mark.asyncio
async def test_query_failure_on_one_task_keeps_other_task_evidence() -> None:
    failed = _task("failed query")
    succeeded = _task("successful query")
    selector_runtime = FakeRuntime(
        [
            _selection_draft(
                [{"candidate_index": 0, "claim": "claim", "why_selected": "why"}]
            )
        ]
    )
    result = await _runner(
        query_runtime=FakeRuntime(
            [
                AgentResponseInvalidError(AgentResponseDefect.RESPONSE_NOT_JSON),
                _query_draft(["q"]),
            ]
        ),
        search_provider=FakeSearchProvider(
            {"q": [_candidate("https://example.com/q")]}
        ),
        selector_runtime=selector_runtime,
    ).search(_request([failed, succeeded], effective_agent_count=1))

    assert [report.status for report in result.task_reports] == [
        "query_generation_failed",
        "succeeded",
    ]
    assert [evidence.task_index for evidence in result.evidence] == [1]


@pytest.mark.asyncio
async def test_candidate_pool_round_robins_deduplicates_urls_and_applies_pool_cap() -> (
    None
):
    pool_limit = _required_attribute(
        _contracts(), "EXTERNAL_SEARCH_CANDIDATE_POOL_LIMIT_PER_TASK"
    )
    query_runtime = FakeRuntime([_query_draft(["q1", "q2", "q3"])])
    selector_runtime = FakeRuntime([_selection_draft([])])
    search_provider = FakeSearchProvider(
        {
            query: [
                _candidate(f"https://example.com/{query}-{index}")
                for index in range(pool_limit)
            ]
            for query in ("q1", "q2", "q3")
        }
    )
    result = await _runner(
        query_runtime=query_runtime,
        search_provider=search_provider,
        selector_runtime=selector_runtime,
    ).search(_request([_task("pool")]))

    candidates = selector_runtime.calls[0].input.candidates
    assert len(candidates) == pool_limit
    assert [candidate.index for candidate in candidates[:6]] == list(range(6))
    assert [candidate.title for candidate in candidates[:6]] == [
        "q1-0",
        "q2-0",
        "q3-0",
        "q1-1",
        "q2-1",
        "q3-1",
    ]
    assert result.task_reports[0].candidate_count == pool_limit


@pytest.mark.asyncio
async def test_candidate_pool_removes_duplicate_urls_before_selector_projection() -> (
    None
):
    selector_runtime = FakeRuntime([_selection_draft([])])
    result = await _runner(
        query_runtime=FakeRuntime([_query_draft(["q1", "q2"])]),
        search_provider=FakeSearchProvider(
            {
                "q1": [
                    _candidate("https://example.com/q1-0"),
                    _candidate("https://example.com/shared"),
                ],
                "q2": [
                    _candidate("https://example.com/q2-0"),
                    _candidate("https://example.com/shared"),
                ],
            }
        ),
        selector_runtime=selector_runtime,
    ).search(_request([_task("dedupe")]))

    candidate_titles = [
        candidate.title for candidate in selector_runtime.calls[0].input.candidates
    ]
    assert candidate_titles == [
        "q1-0",
        "q2-0",
        "shared",
    ]
    assert result.task_reports[0].candidate_count == 3


@pytest.mark.asyncio
async def test_provider_result_cap_is_applied_before_candidate_pool() -> None:
    candidate_limit = _required_attribute(
        _contracts(), "EXTERNAL_SEARCH_CANDIDATES_PER_QUERY"
    )
    selector_runtime = FakeRuntime([_selection_draft([])])
    result = await _runner(
        query_runtime=FakeRuntime([_query_draft(["q"])]),
        search_provider=FakeSearchProvider(
            {
                "q": [
                    _candidate(f"https://example.com/candidate-{index}")
                    for index in range(candidate_limit + 3)
                ]
            }
        ),
        selector_runtime=selector_runtime,
    ).search(_request([_task("provider result cap")]))

    candidates = selector_runtime.calls[0].input.candidates
    assert len(candidates) == candidate_limit
    assert candidates[-1].title == f"candidate-{candidate_limit - 1}"
    assert result.task_reports[0].candidate_count == candidate_limit


@pytest.mark.asyncio
async def test_selection_cap_preserves_candidate_index_source_refs() -> None:
    evidence_limit = _required_attribute(
        _contracts(), "EXTERNAL_SEARCH_EVIDENCE_LIMIT_PER_TASK"
    )
    selections = [
        {"candidate_index": 2, "claim": "two", "why_selected": "why"},
        {"candidate_index": 0, "claim": "zero", "why_selected": "why"},
        {"candidate_index": 99, "claim": "bad", "why_selected": "why"},
        {"candidate_index": 2, "claim": "duplicate", "why_selected": "why"},
    ] + [
        {"candidate_index": index, "claim": f"claim {index}", "why_selected": "why"}
        for index in range(1, evidence_limit + 2)
    ]
    selector_runtime = FakeRuntime([_selection_draft(selections)])
    result = await _runner(
        query_runtime=FakeRuntime([_query_draft(["q"])]),
        search_provider=FakeSearchProvider(
            {
                "q": [
                    _candidate(f"https://example.com/candidate-{index}")
                    for index in range(evidence_limit + 2)
                ]
            }
        ),
        selector_runtime=selector_runtime,
    ).search(_request([_task("selection cap")]))

    assert result.task_reports[0].evidence_count == evidence_limit
    assert result.task_reports[0].dropped_selection_count == 5
    assert [item.source_ref for item in result.evidence] == [
        "external-0-2",
        "external-0-0",
        "external-0-1",
        "external-0-3",
        "external-0-4",
    ]


@pytest.mark.asyncio
async def test_empty_selection_is_successful_and_reports_evidence_selected_event() -> (
    None
):
    reporter = FakeEventReporter()
    result = await _runner(
        query_runtime=FakeRuntime([_query_draft(["q"])]),
        search_provider=FakeSearchProvider(
            {"q": [_candidate("https://example.com/q")]}
        ),
        selector_runtime=FakeRuntime([_selection_draft([])]),
        events=reporter,
    ).search(_request([_task("empty selection")]))

    assert result.evidence == []
    assert result.task_reports[0].status == "succeeded"
    assert result.task_reports[0].evidence_count == 0
    assert [event.type for event in reporter.events] == [
        "external_search.queries_generated",
        "external_search.candidates_fetched",
        "external_search.evidence_selected",
    ]
    assert reporter.events[-1].evidence_count == 0


def test_selection_result_factory_and_direct_models_preserve_existing_caps() -> None:
    contracts = _contracts()
    result_type = _required_attribute(contracts, "EvidenceSelectionResult")
    selection_type = _required_attribute(contracts, "EvidenceSelection")
    claim_limit = _required_attribute(contracts, "EVIDENCE_CLAIM_MAX_CHARS")
    why_limit = _required_attribute(contracts, "EVIDENCE_WHY_SELECTED_MAX_CHARS")
    missing_item_limit = _required_attribute(contracts, "MISSING_ITEM_MAX_CHARS")
    missing_count_limit = _required_attribute(
        contracts, "EXTERNAL_SEARCH_MISSING_LIMIT_PER_TASK"
    )
    result = result_type.from_raw(
        selections=[
            {
                "candidate_index": 0,
                "claim": "c" * (claim_limit + 1),
                "why_selected": "w" * (why_limit + 1),
            }
        ],
        missing=[
            "m" * (missing_item_limit + 1) for _ in range(missing_count_limit + 1)
        ],
    )

    assert len(result.selections[0].claim) == claim_limit
    assert len(result.selections[0].why_selected) == why_limit
    assert len(result.missing) == missing_count_limit
    with pytest.raises(ValidationError):
        selection_type(
            candidate_index=0,
            claim="c" * (claim_limit + 1),
            why_selected="why",
        )
    with pytest.raises(ValidationError):
        result_type(selections=[], missing=["m" * (missing_item_limit + 1)])
