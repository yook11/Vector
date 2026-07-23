"""QuestionPlanningService の phase / provider span 契約。

Gemini SDK I/O だけを fake にし、productionの親子関係・retry・非漏洩を検証する。
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import cast

import pytest
from google.genai.client import AsyncClient
from logfire.testing import CaptureLogfire
from opentelemetry.trace import StatusCode

from app.agent.planning.agent import QUESTION_PLANNER_AGENT
from app.agent.planning.contract import PlanningRequest
from app.agent.planning.service import QuestionPlanningService
from app.agent.question_context.contract import QuestionContext
from app.agent.runtime.contract import AgentRuntime
from app.agent.runtime.gemini import GeminiAgentRuntime
from app.logfire.redaction import install_exception_redaction
from tests.agent.runtime._helpers import FakeGeminiClient, FakeResponse
from tests.logfire._span_helpers import (
    domain_attr_keys,
    exception_event,
    one_span_named,
    spans_named,
)

_PHASE_SPAN_NAME = "agent_phase"
_PROVIDER_SPAN_NAME = "agent_provider_call"


def _input(question: str = "今日のNVIDIAの発表は？") -> PlanningRequest:
    return PlanningRequest(
        context=QuestionContext(standalone_question=question),
        as_of=datetime(2026, 6, 29, tzinfo=UTC),
    )


def _successful_response() -> FakeResponse:
    return FakeResponse(
        text=json.dumps(
            {
                "plan_type": "search",
                "article_search_queries": ["NVIDIA の直近発表"],
                "research_goals": ["NVIDIA の直近発表の外部根拠を確認する"],
                "target_time_window": None,
            }
        ),
        usage_metadata=SimpleNamespace(
            prompt_token_count=11,
            candidates_token_count=7,
            cached_content_token_count=3,
            thoughts_token_count=2,
        ),
    )


def _service(client: FakeGeminiClient) -> QuestionPlanningService:
    runtime = GeminiAgentRuntime(client=cast(AsyncClient, client))

    @asynccontextmanager
    async def runtime_scope() -> AsyncIterator[AgentRuntime]:
        yield runtime

    return QuestionPlanningService(
        agent=QUESTION_PLANNER_AGENT,
        runtime_scope_factory=runtime_scope,
    )


async def test_success_records_one_production_provider_attempt_without_phase_usage_copy(
    capfire: CaptureLogfire,
) -> None:
    client = FakeGeminiClient([_successful_response()])

    plan = await _service(client).plan(_input())

    phase = one_span_named(capfire, _PHASE_SPAN_NAME)
    provider = one_span_named(capfire, _PROVIDER_SPAN_NAME)
    assert plan.plan_type == "search"
    assert client.models.generate_content.await_count == 1
    assert domain_attr_keys(phase["attributes"]) == {"phase", "agent_name"}
    assert phase["attributes"]["phase"] == "question_planning"
    assert phase["attributes"]["agent_name"] == QUESTION_PLANNER_AGENT.name
    assert provider["attributes"]["attempt_number"] == 1
    assert provider["attributes"]["result"] == "succeeded"
    assert provider["attributes"]["gen_ai.usage.input_tokens"] == 11
    assert provider["parent"]["span_id"] == phase["context"]["span_id"]
    assert provider["context"]["trace_id"] == phase["context"]["trace_id"]
    assert not any(key.startswith("gen_ai.usage.") for key in phase["attributes"])


async def test_invalid_json_then_success_records_two_production_provider_attempts(
    capfire: CaptureLogfire,
) -> None:
    invalid_json_sentinel = "PLANNER_INVALID_JSON_SENTINEL_13ab"
    client = FakeGeminiClient(
        [
            FakeResponse(text=invalid_json_sentinel),
            _successful_response(),
        ]
    )

    plan = await _service(client).plan(_input())

    phase = one_span_named(capfire, _PHASE_SPAN_NAME)
    providers = spans_named(capfire, _PROVIDER_SPAN_NAME)
    assert plan.plan_type == "search"
    assert client.models.generate_content.await_count == 2
    assert [provider["attributes"]["attempt_number"] for provider in providers] == [
        1,
        2,
    ]
    assert [provider["attributes"]["result"] for provider in providers] == [
        "invalid_response",
        "succeeded",
    ]
    assert all(
        provider["parent"]["span_id"] == phase["context"]["span_id"]
        for provider in providers
    )
    assert exception_event(phase) is None
    assert phase.get("status", {}).get("description") in (None, "")
    assert invalid_json_sentinel not in json.dumps(
        [phase, *providers], default=str, ensure_ascii=False
    )


async def test_unknown_error_is_redacted_in_production_phase_and_provider_spans(
    capfire: CaptureLogfire,
) -> None:
    install_exception_redaction()
    error_sentinel = "UNCLASSIFIED_PLANNER_SENTINEL_1f78"
    question_sentinel = "QUESTION_SENTINEL_ef34"
    error = RuntimeError(error_sentinel)
    client = FakeGeminiClient([error])

    with pytest.raises(RuntimeError) as raised:
        await _service(client).plan(_input(question_sentinel))

    phase = one_span_named(capfire, _PHASE_SPAN_NAME)
    provider = one_span_named(capfire, _PROVIDER_SPAN_NAME)
    phase_event = exception_event(phase)
    provider_event = exception_event(provider)
    raw_phases = [
        span
        for span in capfire.exporter.exported_spans
        if span.name == _PHASE_SPAN_NAME
        and (span.attributes or {}).get("logfire.span_type") == "span"
    ]
    assert raised.value is error
    assert client.models.generate_content.await_count == 1
    assert provider["parent"]["span_id"] == phase["context"]["span_id"]
    assert phase_event is not None
    assert provider_event is not None
    assert phase_event["attributes"]["exception.message"] == "[redacted]"
    assert phase_event["attributes"]["exception.stacktrace"] == "[redacted]"
    assert provider_event["attributes"]["exception.message"] == "[redacted]"
    assert provider_event["attributes"]["exception.stacktrace"] == "[redacted]"
    assert len(raw_phases) == 1
    assert raw_phases[0].status.status_code is StatusCode.ERROR
    assert raw_phases[0].status.description == "[redacted]"
    assert domain_attr_keys(phase["attributes"]) == {"phase", "agent_name"}
    assert "result" not in provider["attributes"]
    span_dump = json.dumps([phase, provider], default=str, ensure_ascii=False)
    assert error_sentinel not in span_dump
    assert question_sentinel not in span_dump
    assert "run_id" not in span_dump
