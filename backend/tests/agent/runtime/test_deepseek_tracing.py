"""DeepSeekAgentRuntime provider-attempt span 契約。"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest
from logfire.testing import CaptureLogfire
from opentelemetry.trace import SpanKind, StatusCode

from app.logfire.redaction import install_exception_redaction
from tests.agent.runtime._deepseek_helpers import (
    FakeDeepSeekClient,
    function_response,
    make_agent,
    make_binding,
    required_attribute,
    runtime_contract,
    runtime_type,
    success_response,
)

_SPAN_NAME = "agent_provider_call"
_FRAMEWORK_PREFIXES = ("logfire.", "code.")
_STANDARD_ATTRIBUTE_KEYS = {
    "error.type",
    "gen_ai.operation.name",
    "gen_ai.provider.name",
    "gen_ai.request.model",
    "gen_ai.response.model",
    "gen_ai.usage.input_tokens",
    "gen_ai.usage.output_tokens",
    "gen_ai.usage.cache_read.input_tokens",
    "gen_ai.usage.reasoning.output_tokens",
}


def _runtime_spans(capfire: CaptureLogfire) -> list[Any]:
    return [
        span
        for span in capfire.exporter.exported_spans
        if span.name == _SPAN_NAME
        and (span.attributes or {}).get("logfire.span_type") == "span"
    ]


def _one_runtime_span(capfire: CaptureLogfire) -> Any:
    spans = _runtime_spans(capfire)
    assert len(spans) == 1, f"expected one {_SPAN_NAME} span, got {len(spans)}"
    return spans[0]


def _application_attribute_keys(span: Any) -> set[str]:
    return {
        key
        for key in (span.attributes or {})
        if not key.startswith(_FRAMEWORK_PREFIXES)
        and key not in _STANDARD_ATTRIBUTE_KEYS
    }


def _span_text(span: Any) -> str:
    values = [span.status.description or ""]
    values.extend(str(value) for value in (span.attributes or {}).values())
    for event in span.events:
        values.extend(str(value) for value in (event.attributes or {}).values())
    return "\n".join(values)


def _exception_events(span: Any) -> list[Any]:
    return [event for event in span.events if event.name == "exception"]


def _usage() -> SimpleNamespace:
    return SimpleNamespace(
        prompt_tokens=11,
        completion_tokens=7,
        prompt_tokens_details=SimpleNamespace(cached_tokens=3),
        completion_tokens_details=SimpleNamespace(reasoning_tokens=2),
    )


async def test_success_span_has_only_allowlisted_agent_attributes_and_no_text(
    capfire: CaptureLogfire,
) -> None:
    model_output_sentinel = "MODEL_OUTPUT_SENTINEL_SUCCESS_83cd"
    response_model_sentinel = "PROVIDER_RESPONSE_MODEL_SENTINEL_617d"
    agent = make_agent(
        name="deepseek_trace_agent",
        instructions="SYSTEM_INSTRUCTIONS_SENTINEL_TRACE_e96b",
        rendered_input="TASK_CONTENTS_SENTINEL_TRACE_19a2",
    )
    client = FakeDeepSeekClient(
        [
            success_response(
                result=model_output_sentinel,
                usage=_usage(),
                model=response_model_sentinel,
            )
        ]
    )

    await runtime_type()(client=client, binding=make_binding()).invoke(
        agent, object(), attempt_number=2
    )

    span = _one_runtime_span(capfire)
    attributes = dict(span.attributes or {})
    assert span.kind is SpanKind.CLIENT
    assert _application_attribute_keys(span) == {
        "agent_name",
        "attempt_number",
        "result",
    }
    assert attributes["agent_name"] == "deepseek_trace_agent"
    assert attributes["attempt_number"] == 2
    assert attributes["result"] == "succeeded"
    assert attributes["gen_ai.provider.name"] == "deepseek"
    assert attributes["gen_ai.request.model"] == "deepseek-v4-flash"
    assert attributes["gen_ai.usage.input_tokens"] == 11
    assert attributes["gen_ai.usage.output_tokens"] == 7
    assert attributes["gen_ai.usage.cache_read.input_tokens"] == 3
    assert attributes["gen_ai.usage.reasoning.output_tokens"] == 2
    assert "run_id" not in attributes
    for sentinel in (
        "SYSTEM_INSTRUCTIONS_SENTINEL_TRACE_e96b",
        "TASK_CONTENTS_SENTINEL_TRACE_19a2",
        "PROMPT_VERSION_SENTINEL_v1",
        model_output_sentinel,
        response_model_sentinel,
    ):
        assert sentinel not in _span_text(span)


async def test_invalid_response_records_usage_before_safe_classification(
    capfire: CaptureLogfire,
) -> None:
    contract = runtime_contract()
    error_type = required_attribute(contract, "AgentResponseInvalidError")
    client = FakeDeepSeekClient(
        [
            function_response(
                arguments="MODEL_OUTPUT_INVALID_JSON_SENTINEL_041b",
                usage=_usage(),
            )
        ]
    )

    with pytest.raises(error_type):
        await runtime_type()(client=client, binding=make_binding()).invoke(
            make_agent(), object(), attempt_number=1
        )

    span = _one_runtime_span(capfire)
    attributes = dict(span.attributes or {})
    assert attributes["result"] == "invalid_response"
    assert isinstance(attributes["error.type"], str)
    assert attributes["gen_ai.usage.input_tokens"] == 11
    assert attributes["gen_ai.usage.output_tokens"] == 7
    assert attributes["gen_ai.usage.cache_read.input_tokens"] == 3
    assert attributes["gen_ai.usage.reasoning.output_tokens"] == 2
    assert span.status.status_code is StatusCode.ERROR
    assert span.status.description in (None, "")
    assert _exception_events(span) == []
    assert "MODEL_OUTPUT_INVALID_JSON_SENTINEL_041b" not in _span_text(span)


async def test_schema_mismatch_span_does_not_record_model_output_or_exception_event(
    capfire: CaptureLogfire,
) -> None:
    contract = runtime_contract()
    error_type = required_attribute(contract, "AgentResponseInvalidError")
    sentinel = "MODEL_OUTPUT_SCHEMA_SENTINEL_0bea"
    client = FakeDeepSeekClient(
        [
            function_response(
                arguments=json.dumps({"result": sentinel, "unexpected": sentinel}),
                usage=_usage(),
            )
        ]
    )

    with pytest.raises(error_type):
        await runtime_type()(client=client, binding=make_binding()).invoke(
            make_agent(), object(), attempt_number=1
        )

    span = _one_runtime_span(capfire)
    assert _exception_events(span) == []
    assert span.status.description in (None, "")
    assert sentinel not in _span_text(span)


async def test_unclassified_error_keeps_redacted_exception_event_without_result(
    capfire: CaptureLogfire,
) -> None:
    install_exception_redaction()
    error = RuntimeError("UNCLASSIFIED_DEEPSEEK_SENTINEL_35ce")
    client = FakeDeepSeekClient([error])

    with pytest.raises(RuntimeError) as raised:
        await runtime_type()(client=client, binding=make_binding()).invoke(
            make_agent(), object(), attempt_number=1
        )

    span = _one_runtime_span(capfire)
    attributes = dict(span.attributes or {})
    events = _exception_events(span)
    assert raised.value is error
    assert "result" not in attributes
    assert len(events) == 1
    assert events[0].attributes["exception.message"] == "[redacted]"
    assert events[0].attributes["exception.stacktrace"] == "[redacted]"
    assert span.status.status_code is StatusCode.ERROR
    assert span.status.description == "[redacted]"
    assert "UNCLASSIFIED_DEEPSEEK_SENTINEL_35ce" not in _span_text(span)


async def test_renderer_failure_creates_no_provider_span(
    capfire: CaptureLogfire,
) -> None:
    error = RuntimeError("RENDERER_FAILURE_SENTINEL_7a1d")
    client = FakeDeepSeekClient([success_response()])
    agent = make_agent()
    agent = agent.__class__(
        name=agent.name,
        prompt=agent.prompt.__class__(
            version=agent.prompt.version,
            instructions=agent.prompt.instructions,
            input_renderer=lambda _input: (_ for _ in ()).throw(error),
        ),
        model=agent.model,
        model_settings=agent.model_settings,
        output_type=agent.output_type,
        response_schema=agent.response_schema,
    )

    with pytest.raises(RuntimeError) as raised:
        await runtime_type()(client=client, binding=make_binding()).invoke(
            agent, object(), attempt_number=1
        )

    assert raised.value is error
    assert _runtime_spans(capfire) == []
    client.chat.completions.create.assert_not_awaited()
