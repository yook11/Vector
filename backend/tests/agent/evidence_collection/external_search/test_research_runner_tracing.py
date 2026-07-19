"""External Query / Selector の phase / provider span 契約。

DeepSeek SDK I/O だけを fake にし、productionの親子関係・retry・非漏洩を検証する。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import cast

import pytest
from logfire.testing import CaptureLogfire
from openai import AsyncOpenAI
from opentelemetry.trace import StatusCode

from app.agent.evidence_collection.external_search.agent import (
    EXTERNAL_EVIDENCE_SELECTOR_AGENT,
    EXTERNAL_QUERY_AGENT,
)
from app.agent.evidence_collection.external_search.contract import (
    ExternalSearchCandidate,
    ExternalSearchRequest,
    ExternalSearchToolInput,
)
from app.agent.evidence_collection.external_search.deepseek_binding import (
    EXTERNAL_EVIDENCE_SELECTOR_DEEPSEEK_BINDING,
    EXTERNAL_QUERY_DEEPSEEK_BINDING,
)
from app.agent.evidence_collection.external_search.runner import (
    ExternalSearchResearchRunner,
)
from app.agent.planning.contract import ExternalResearchTask
from app.agent.runtime.deepseek import DeepSeekAgentRuntime
from app.logfire.redaction import install_exception_redaction
from tests.agent.runtime._deepseek_helpers import (
    FakeDeepSeekClient,
    function_response,
)
from tests.logfire._span_helpers import domain_attr_keys, exception_event, spans_named

_PHASE_SPAN_NAME = "agent_phase"
_PROVIDER_SPAN_NAME = "agent_provider_call"
_QUERY_OUTPUT_SENTINEL = "GENERATED_QUERY_SENTINEL_1f24"
_SELECTION_CLAIM_SENTINEL = "SELECTION_CLAIM_SENTINEL_98ab"
_SELECTION_WHY_SENTINEL = "SELECTION_WHY_SENTINEL_7c31"


def _request() -> ExternalSearchRequest:
    return ExternalSearchRequest(
        tasks=[ExternalResearchTask(collection_goal="GOAL_SENTINEL_3cc7")],
        effective_agent_count=1,
        as_of=datetime(2026, 7, 19, tzinfo=UTC),
        target_time_window="WINDOW_SENTINEL_9b28",
    )


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


class FakeExternalSearchTool:
    """検索 I/O を差し替え、Runtimeが生成した query の受け渡しを記録する。"""

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


def _runner(
    *,
    query_client: FakeDeepSeekClient,
    selector_client: FakeDeepSeekClient,
    search_tool: FakeExternalSearchTool | None = None,
) -> ExternalSearchResearchRunner:
    return ExternalSearchResearchRunner(
        query_agent=EXTERNAL_QUERY_AGENT,
        query_runtime=DeepSeekAgentRuntime(
            client=cast(AsyncOpenAI, query_client),
            binding=EXTERNAL_QUERY_DEEPSEEK_BINDING,
        ),
        search_tool=search_tool or FakeExternalSearchTool(),
        selector_agent=EXTERNAL_EVIDENCE_SELECTOR_AGENT,
        selector_runtime=DeepSeekAgentRuntime(
            client=cast(AsyncOpenAI, selector_client),
            binding=EXTERNAL_EVIDENCE_SELECTOR_DEEPSEEK_BINDING,
        ),
    )


async def test_query_and_selector_production_spans_preserve_phase_boundaries(
    capfire: CaptureLogfire,
) -> None:
    raw_selector_response_sentinel = "RAW_SELECTOR_RESPONSE_SENTINEL_5d71"
    query_client = FakeDeepSeekClient([_query_response()])
    search_tool = FakeExternalSearchTool()
    selector_client = FakeDeepSeekClient(
        [
            function_response(
                function_name=(
                    EXTERNAL_EVIDENCE_SELECTOR_DEEPSEEK_BINDING.function_name
                ),
                arguments=raw_selector_response_sentinel,
                usage=_usage(),
            ),
            _selector_response(),
        ]
    )

    await _runner(
        query_client=query_client,
        selector_client=selector_client,
        search_tool=search_tool,
    ).search(_request())

    phases = spans_named(capfire, _PHASE_SPAN_NAME)
    providers = spans_named(capfire, _PROVIDER_SPAN_NAME)
    phase_by_agent = {phase["attributes"]["agent_name"]: phase for phase in phases}
    assert query_client.chat.completions.create.await_count == 1
    assert selector_client.chat.completions.create.await_count == 2
    assert [input.query for input in search_tool.inputs] == [_QUERY_OUTPUT_SENTINEL]
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
    assert all("prompt_version" not in phase["attributes"] for phase in phases)
    assert all(
        not any(key.startswith("gen_ai.usage.") for key in phase["attributes"])
        for phase in phases
    )
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
    assert [provider["attributes"]["prompt_version"] for provider in providers] == [
        EXTERNAL_QUERY_AGENT.prompt.version,
        EXTERNAL_EVIDENCE_SELECTOR_AGENT.prompt.version,
        EXTERNAL_EVIDENCE_SELECTOR_AGENT.prompt.version,
    ]
    assert all(
        "gen_ai.usage.input_tokens" in provider["attributes"] for provider in providers
    )
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
    trace_dump = json.dumps(
        capfire.exporter.exported_spans_as_dict(),
        ensure_ascii=False,
        default=str,
    )
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


async def test_unknown_query_error_is_redacted_in_production_phase_and_provider(
    capfire: CaptureLogfire,
) -> None:
    install_exception_redaction()
    error_sentinel = "UNCLASSIFIED_QUERY_ERROR_SENTINEL_4ea2"
    error = RuntimeError(error_sentinel)
    query_client = FakeDeepSeekClient([error])
    selector_client = FakeDeepSeekClient([_selector_response()])

    with pytest.raises(RuntimeError) as raised:
        await _runner(
            query_client=query_client,
            selector_client=selector_client,
        ).search(_request())

    phases = spans_named(capfire, _PHASE_SPAN_NAME)
    providers = spans_named(capfire, _PROVIDER_SPAN_NAME)
    assert raised.value is error
    assert len(phases) == 1
    assert len(providers) == 1
    phase = phases[0]
    provider = providers[0]
    assert phase["attributes"]["agent_name"] == EXTERNAL_QUERY_AGENT.name
    assert provider["parent"]["span_id"] == phase["context"]["span_id"]
    assert (
        provider["attributes"]["prompt_version"] == EXTERNAL_QUERY_AGENT.prompt.version
    )
    assert "result" not in provider["attributes"]
    assert selector_client.chat.completions.create.await_count == 0
    for span in (phase, provider):
        event = exception_event(span)
        assert event is not None
        assert event["attributes"]["exception.message"] == "[redacted]"
        assert event["attributes"]["exception.stacktrace"] == "[redacted]"
    raw_spans = [
        span
        for span in capfire.exporter.exported_spans
        if span.name in {_PHASE_SPAN_NAME, _PROVIDER_SPAN_NAME}
        and (span.attributes or {}).get("logfire.span_type") == "span"
    ]
    assert len(raw_spans) == 2
    assert all(span.status.status_code is StatusCode.ERROR for span in raw_spans)
    assert all(span.status.description == "[redacted]" for span in raw_spans)
    trace_dump = json.dumps(
        capfire.exporter.exported_spans_as_dict(),
        ensure_ascii=False,
        default=str,
    )
    assert error_sentinel not in trace_dump
    assert "GOAL_SENTINEL_3cc7" not in trace_dump
    assert "WINDOW_SENTINEL_9b28" not in trace_dump
