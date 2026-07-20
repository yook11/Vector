"""AnsweringRunner 所有の external phase trace 契約。"""

from __future__ import annotations

import json
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID

import pytest
from logfire.testing import CaptureLogfire
from openai import AsyncOpenAI
from opentelemetry.trace import StatusCode

from app.agent.answering.contract import AnsweringRequest
from app.agent.answering.direct_answer.contract import DirectAnswerDraft
from app.agent.answering.evidence_answer.contract import EvidenceAnswerDraft
from app.agent.evidence_collection.external_search.agent import (
    EXTERNAL_EVIDENCE_SELECTOR_AGENT,
    EXTERNAL_QUERY_AGENT,
)
from app.agent.evidence_collection.external_search.contract import (
    ExternalResearchRuntime,
    ExternalSearchCandidate,
    ExternalSearchToolInput,
)
from app.agent.evidence_collection.external_search.deepseek_binding import (
    EXTERNAL_EVIDENCE_SELECTOR_DEEPSEEK_BINDING,
    EXTERNAL_QUERY_DEEPSEEK_BINDING,
)
from app.agent.planning.contract import (
    ExternalResearchTask,
    ExternalSearchPlan,
    PlanningRequest,
)
from app.agent.question_context import (
    QuestionContext,
    QuestionContextPreparationResult,
    QuestionContextTelemetry,
)
from app.agent.running import AnsweringPhases, AnsweringRunner, RunContext, RunInput
from app.agent.runtime.deepseek import DeepSeekAgentRuntime
from app.logfire.redaction import install_exception_redaction
from tests.agent.runtime._deepseek_helpers import FakeDeepSeekClient, function_response
from tests.logfire._span_helpers import domain_attr_keys, exception_event, spans_named

_PHASE_SPAN_NAME = "agent_phase"
_PROVIDER_SPAN_NAME = "agent_provider_call"
_QUERY_OUTPUT_SENTINEL = "GENERATED_QUERY_SENTINEL_1f24"
_SELECTION_CLAIM_SENTINEL = "SELECTION_CLAIM_SENTINEL_98ab"
_SELECTION_WHY_SENTINEL = "SELECTION_WHY_SENTINEL_7c31"


def _usage() -> SimpleNamespace:
    return SimpleNamespace(
        prompt_tokens=11,
        completion_tokens=7,
        prompt_tokens_details=SimpleNamespace(cached_tokens=3),
        completion_tokens_details=SimpleNamespace(reasoning_tokens=2),
    )


def _query_response() -> object:
    return function_response(
        function_name=EXTERNAL_QUERY_DEEPSEEK_BINDING.function_name,
        arguments=json.dumps({"queries": [_QUERY_OUTPUT_SENTINEL]}),
        usage=_usage(),
    )


def _selector_response() -> object:
    return function_response(
        function_name=EXTERNAL_EVIDENCE_SELECTOR_DEEPSEEK_BINDING.function_name,
        arguments=json.dumps(
            {
                "selections": [
                    {
                        "candidate_index": 0,
                        "claim": _SELECTION_CLAIM_SENTINEL,
                        "why_selected": _SELECTION_WHY_SENTINEL,
                    }
                ],
                "missing": [],
            }
        ),
        usage=_usage(),
    )


class _Preparer:
    async def prepare(self, **_kwargs: object) -> QuestionContextPreparationResult:
        return QuestionContextPreparationResult(
            context=QuestionContext(standalone_question="NVIDIA の見通しは？"),
            telemetry=QuestionContextTelemetry(),
        )


class _Planner:
    async def plan(self, request: PlanningRequest) -> ExternalSearchPlan:
        del request
        return ExternalSearchPlan(
            external_research_tasks=[
                ExternalResearchTask(collection_goal="GOAL_SENTINEL_3cc7")
            ],
            target_time_window="WINDOW_SENTINEL_9b28",
            reason="trace external pipeline",
        )


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
    async def answer(
        self,
        *,
        request: AnsweringRequest,
        evidence: list[Any],
        target_time_window: str | None,
    ) -> EvidenceAnswerDraft:
        del request, target_time_window
        return EvidenceAnswerDraft(
            sufficiency="answered",
            answer="根拠に基づく回答です。",
            cited_refs=[item.source.source_ref for item in evidence],
        )


class _Tool:
    def __init__(self) -> None:
        self.inputs: list[ExternalSearchToolInput] = []

    @property
    def name(self) -> str:
        return "external_search"

    async def invoke(
        self, input: ExternalSearchToolInput
    ) -> list[ExternalSearchCandidate]:
        self.inputs.append(input)
        return [
            ExternalSearchCandidate(
                url="https://example.com/TRACE_URL_SENTINEL_63df",
                title="CANDIDATE_TITLE_SENTINEL_4cab",
                snippet="CANDIDATE_SNIPPET_SENTINEL_00f4",
                source_name="Example",
            )
        ]


class _Scope(AbstractAsyncContextManager[ExternalResearchRuntime]):
    def __init__(self, runtime: ExternalResearchRuntime) -> None:
        self._runtime = runtime

    async def __aenter__(self) -> ExternalResearchRuntime:
        return self._runtime

    async def __aexit__(
        self,
        exc_type: object,
        exc: BaseException | None,
        traceback: object,
    ) -> bool:
        del exc_type, exc, traceback
        return False


class _Factory:
    def __init__(self, runtime: ExternalResearchRuntime) -> None:
        self._runtime = runtime

    def activate(self) -> _Scope:
        return _Scope(self._runtime)


def _runner(
    *,
    query_client: FakeDeepSeekClient,
    selector_client: FakeDeepSeekClient,
    search_tool: _Tool | None = None,
) -> tuple[AnsweringRunner, _Tool]:
    tool = search_tool or _Tool()
    runtime = ExternalResearchRuntime(
        query_runtime=DeepSeekAgentRuntime(
            client=cast(AsyncOpenAI, query_client),
            binding=EXTERNAL_QUERY_DEEPSEEK_BINDING,
        ),
        selector_runtime=DeepSeekAgentRuntime(
            client=cast(AsyncOpenAI, selector_client),
            binding=EXTERNAL_EVIDENCE_SELECTOR_DEEPSEEK_BINDING,
        ),
        search_tool=tool,
    )
    phases = AnsweringPhases(
        planner=_Planner(),
        internal_search=_UnreachableInternalSearch(),
        external_runtime_factory=_Factory(runtime),
        direct_answerer=_UnreachableDirectAnswerer(),
        evidence_answerer=_EvidenceAnswerer(),
    )
    return (
        AnsweringRunner(context_preparer=_Preparer(), phases_factory=lambda: phases),
        tool,
    )


async def _run(runner: AnsweringRunner) -> None:
    await runner.run(
        RunInput(question="NVIDIA の見通しは？", history=()),
        run_context=RunContext(
            run_id=UUID("019bd239-1ed4-7fbb-a336-04fe3c197652"),
            as_of=datetime(2026, 7, 19, tzinfo=UTC),
        ),
    )


@pytest.mark.asyncio
async def test_external_phase_spans_keep_attributes_parentage_and_no_sensitive_trace(
    capfire: CaptureLogfire,
) -> None:
    raw_selector_response_sentinel = "RAW_SELECTOR_RESPONSE_SENTINEL_5d71"
    query_client = FakeDeepSeekClient([_query_response()])
    selector_client = FakeDeepSeekClient(
        [
            function_response(
                function_name=EXTERNAL_EVIDENCE_SELECTOR_DEEPSEEK_BINDING.function_name,
                arguments=raw_selector_response_sentinel,
                usage=_usage(),
            ),
            _selector_response(),
        ]
    )
    runner, tool = _runner(
        query_client=query_client,
        selector_client=selector_client,
    )

    await _run(runner)

    phases = spans_named(capfire, _PHASE_SPAN_NAME)
    providers = spans_named(capfire, _PROVIDER_SPAN_NAME)
    phase_by_agent = {phase["attributes"]["agent_name"]: phase for phase in phases}
    trace_dump = json.dumps(
        capfire.exporter.exported_spans_as_dict(), ensure_ascii=False, default=str
    )
    assert query_client.chat.completions.create.await_count == 1
    assert selector_client.chat.completions.create.await_count == 2
    assert [input.query for input in tool.inputs] == [_QUERY_OUTPUT_SENTINEL]
    assert len(phases) == 2
    assert len(providers) == 3
    assert set(phase_by_agent) == {
        EXTERNAL_QUERY_AGENT.name,
        EXTERNAL_EVIDENCE_SELECTOR_AGENT.name,
    }
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
    assert [provider["attributes"]["result"] for provider in providers] == [
        "succeeded",
        "invalid_response",
        "succeeded",
    ]
    assert (
        providers[0]["parent"]["span_id"]
        == phase_by_agent[EXTERNAL_QUERY_AGENT.name]["context"]["span_id"]
    )
    assert all(
        provider["parent"]["span_id"]
        == phase_by_agent[EXTERNAL_EVIDENCE_SELECTOR_AGENT.name]["context"]["span_id"]
        for provider in providers[1:]
    )
    assert all(exception_event(phase) is None for phase in phases)
    for unsafe in (
        "GOAL_SENTINEL_3cc7",
        "WINDOW_SENTINEL_9b28",
        "TRACE_URL_SENTINEL_63df",
        "CANDIDATE_TITLE_SENTINEL_4cab",
        "CANDIDATE_SNIPPET_SENTINEL_00f4",
        raw_selector_response_sentinel,
        _QUERY_OUTPUT_SENTINEL,
        _SELECTION_CLAIM_SENTINEL,
        _SELECTION_WHY_SENTINEL,
    ):
        assert unsafe not in trace_dump


@pytest.mark.asyncio
async def test_unclassified_query_error_is_redacted_and_only_error_phase(
    capfire: CaptureLogfire,
) -> None:
    install_exception_redaction()
    error_sentinel = "UNCLASSIFIED_QUERY_ERROR_SENTINEL_4ea2"
    error = RuntimeError(error_sentinel)
    query_client = FakeDeepSeekClient([error])
    selector_client = FakeDeepSeekClient([_selector_response()])
    runner, _ = _runner(
        query_client=query_client,
        selector_client=selector_client,
    )

    with pytest.raises(RuntimeError) as raised:
        await _run(runner)

    phases = spans_named(capfire, _PHASE_SPAN_NAME)
    providers = spans_named(capfire, _PROVIDER_SPAN_NAME)
    raw_spans = [
        span
        for span in capfire.exporter.exported_spans
        if span.name in {_PHASE_SPAN_NAME, _PROVIDER_SPAN_NAME}
        and (span.attributes or {}).get("logfire.span_type") == "span"
    ]
    trace_dump = json.dumps(
        capfire.exporter.exported_spans_as_dict(), ensure_ascii=False, default=str
    )
    assert raised.value is error
    assert len(phases) == 1
    assert len(providers) == 1
    assert providers[0]["parent"]["span_id"] == phases[0]["context"]["span_id"]
    assert selector_client.chat.completions.create.await_count == 0
    assert all(exception_event(span) is not None for span in [*phases, *providers])
    assert all(span.status.status_code is StatusCode.ERROR for span in raw_spans)
    assert all(span.status.description == "[redacted]" for span in raw_spans)
    assert error_sentinel not in trace_dump
