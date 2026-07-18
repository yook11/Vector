"""External Query / Selector phase span 契約。"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib import import_module
from types import ModuleType
from typing import Any

import logfire
import pytest
from logfire.testing import CaptureLogfire
from opentelemetry.trace import StatusCode

from app.agent.agent import Agent
from app.agent.planning.contract import ExternalResearchTask
from app.agent.runtime.contract import AgentResponseDefect, AgentResponseInvalidError
from tests.logfire._span_helpers import domain_attr_keys, exception_event, spans_named

_PHASE_SPAN_NAME = "agent_phase"
_PROVIDER_SPAN_NAME = "agent_provider_call"


def _required_module(module_name: str) -> ModuleType:
    try:
        return import_module(module_name)
    except ModuleNotFoundError as exc:
        pytest.fail(f"PR2 trace module is missing: {module_name} ({exc.name})")


def _required_attribute(module: ModuleType, name: str) -> Any:
    if not hasattr(module, name):
        pytest.fail(f"PR2 trace contract is missing: {module.__name__}.{name}")
    return getattr(module, name)


def _contracts() -> ModuleType:
    return _required_module("app.agent.evidence_collection.external_search.contract")


def _agents() -> ModuleType:
    return _required_module("app.agent.evidence_collection.external_search.agent")


def _query_agent() -> Agent[Any, Any]:
    return _required_attribute(_agents(), "EXTERNAL_QUERY_AGENT")


def _selector_agent() -> Agent[Any, Any]:
    return _required_attribute(_agents(), "EXTERNAL_EVIDENCE_SELECTOR_AGENT")


def _runner(
    *,
    query_runtime: TracingRuntime,
    selector_runtime: TracingRuntime,
) -> Any:
    runner_type = _required_attribute(
        _required_module("app.agent.evidence_collection.external_search.runner"),
        "ExternalSearchResearchRunner",
    )
    return runner_type(
        query_agent=_query_agent(),
        query_runtime=query_runtime,
        search_provider=FakeSearchProvider(),
        selector_agent=_selector_agent(),
        selector_runtime=selector_runtime,
    )


def _request() -> Any:
    return _required_attribute(_contracts(), "ExternalSearchRequest")(
        tasks=[ExternalResearchTask(collection_goal="GOAL_SENTINEL_3cc7")],
        effective_agent_count=1,
        as_of=datetime(2026, 7, 19, tzinfo=UTC),
        target_time_window="WINDOW_SENTINEL_9b28",
    )


def _query_draft() -> Any:
    return _required_attribute(_contracts(), "ExternalQueryDraft").model_validate(
        {"queries": ["query"]}
    )


def _selector_draft() -> Any:
    return _required_attribute(
        _contracts(), "ExternalEvidenceSelectionDraft"
    ).model_validate(
        {
            "selections": [
                {"candidate_index": 0, "claim": "claim", "why_selected": "why"}
            ],
            "missing": [],
        }
    )


class FakeSearchProvider:
    async def search(self, query: str, *, limit: int) -> list[Any]:
        candidate_type = _required_attribute(_contracts(), "ExternalSearchCandidate")
        return [
            candidate_type(
                url="https://example.com/TRACE_URL_SENTINEL_63df",
                title="CANDIDATE_TITLE_SENTINEL_4cab",
                snippet="CANDIDATE_SNIPPET_SENTINEL_00f4",
                source_name="Example",
            )
        ]


@dataclass(frozen=True, slots=True)
class RuntimeCall:
    agent: Agent[Any, Any]
    input: object
    attempt_number: int


class TracingRuntime:
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
        self.calls.append(RuntimeCall(agent, input, attempt_number))
        outcome = self._outcomes.pop(0)
        deferred_error: BaseException | None = None
        with logfire.span(
            _PROVIDER_SPAN_NAME,
            agent_name=agent.name,
            attempt_number=attempt_number,
        ) as span:
            if isinstance(outcome, AgentResponseInvalidError):
                span.set_attribute("result", "invalid_response")
                span.set_attribute("error.type", outcome.defect.value)
                span.set_status(StatusCode.ERROR)
                deferred_error = outcome
            elif isinstance(outcome, BaseException):
                raise outcome
            else:
                span.set_attribute("result", "succeeded")
                span.set_attribute("gen_ai.usage.input_tokens", 11)
        if deferred_error is not None:
            raise deferred_error
        return outcome  # type: ignore[return-value]


async def test_query_and_selector_phase_spans_wrap_provider_attempts_without_text(
    capfire: CaptureLogfire,
) -> None:
    query_runtime = TracingRuntime([_query_draft()])
    selector_runtime = TracingRuntime(
        [
            AgentResponseInvalidError(AgentResponseDefect.OUTPUT_SCHEMA_MISMATCH),
            _selector_draft(),
        ]
    )

    await _runner(
        query_runtime=query_runtime,
        selector_runtime=selector_runtime,
    ).search(_request())

    phases = spans_named(capfire, _PHASE_SPAN_NAME)
    providers = spans_named(capfire, _PROVIDER_SPAN_NAME)
    phase_by_agent = {phase["attributes"]["agent_name"]: phase for phase in phases}
    assert len(phases) == 2
    assert len(providers) == 3
    assert set(phase_by_agent) == {_query_agent().name, _selector_agent().name}
    assert all(
        domain_attr_keys(phase["attributes"]) == {"phase", "agent_name", "task_index"}
        for phase in phases
    )
    assert all(phase["attributes"]["task_index"] == 0 for phase in phases)
    assert all("task_index" not in provider["attributes"] for provider in providers)
    assert [provider["attributes"]["attempt_number"] for provider in providers] == [
        1,
        1,
        2,
    ]
    assert (
        providers[0]["parent"]["span_id"]
        == phase_by_agent[_query_agent().name]["context"]["span_id"]
    )
    assert all(
        provider["parent"]["span_id"]
        == phase_by_agent[_selector_agent().name]["context"]["span_id"]
        for provider in providers[1:]
    )
    assert all(exception_event(phase) is None for phase in phases)
    trace_dump = json.dumps([*phases, *providers], ensure_ascii=False, default=str)
    for unsafe in (
        "GOAL_SENTINEL_3cc7",
        "WINDOW_SENTINEL_9b28",
        "TRACE_URL_SENTINEL_63df",
        "CANDIDATE_TITLE_SENTINEL_4cab",
        "CANDIDATE_SNIPPET_SENTINEL_00f4",
        _query_agent().prompt.version,
    ):
        assert unsafe not in trace_dump
