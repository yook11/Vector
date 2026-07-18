"""GeminiAgentRuntime provider-attempt span contract tests."""

from __future__ import annotations

import json
from dataclasses import replace
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from logfire.testing import CaptureLogfire
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.trace import SpanKind, StatusCode

from app.analysis.ai_provider_errors import (
    AIProviderNetworkError,
    AIProviderOutputBlockedError,
)
from app.logfire.redaction import install_exception_redaction
from tests.agent.runtime._helpers import (
    FakeGeminiClient,
    FakeResponse,
    ValidationProbeOutput,
    blocked_response,
    make_agent,
    required_attribute,
    required_module,
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
_DEFAULT_MODEL_VISIBLE_SENTINELS = (
    "SYSTEM_INSTRUCTIONS_SENTINEL_5f21",
    "TASK_CONTENTS_SENTINEL_8a43",
    "prompt-version-sentinel-v1",
)


def _runtime_spans(capfire: CaptureLogfire) -> list[ReadableSpan]:
    return [
        span
        for span in capfire.exporter.exported_spans
        if span.name == _SPAN_NAME
        and (span.attributes or {}).get("logfire.span_type") == "span"
    ]


def _one_runtime_span(capfire: CaptureLogfire) -> ReadableSpan:
    spans = _runtime_spans(capfire)
    assert len(spans) == 1, f"expected one {_SPAN_NAME} span, got {len(spans)}"
    return spans[0]


def _application_attribute_keys(span: ReadableSpan) -> set[str]:
    return {
        key
        for key in (span.attributes or {})
        if not key.startswith(_FRAMEWORK_PREFIXES)
        and key not in _STANDARD_ATTRIBUTE_KEYS
    }


def _span_text(span: ReadableSpan) -> str:
    parts = [span.status.description or ""]
    parts.extend(str(value) for value in (span.attributes or {}).values())
    for event in span.events:
        parts.extend(str(value) for value in (event.attributes or {}).values())
    return "\n".join(parts)


def _exception_events(span: ReadableSpan) -> list[Any]:
    return [event for event in span.events if event.name == "exception"]


def _assert_no_model_visible_text(
    span: ReadableSpan,
    *additional_sentinels: str,
) -> None:
    span_text = _span_text(span)
    assert all(
        sentinel not in span_text
        for sentinel in (*_DEFAULT_MODEL_VISIBLE_SENTINELS, *additional_sentinels)
    )


def _full_usage() -> SimpleNamespace:
    return SimpleNamespace(
        prompt_token_count=11,
        candidates_token_count=7,
        cached_content_token_count=3,
        thoughts_token_count=2,
        total_token_count=18,
    )


async def test_success_span_is_client_with_allowlisted_attributes_and_no_text(
    capfire: CaptureLogfire,
) -> None:
    model_output_sentinel = "MODEL_OUTPUT_SENTINEL_SUCCESS_83cd"
    agent = make_agent(
        name="trace_agent",
        instructions="SYSTEM_INSTRUCTIONS_SENTINEL_TRACE_e96b",
        rendered_input="TASK_CONTENTS_SENTINEL_TRACE_19a2",
        model_name="gemini-trace-model",
    )
    client = FakeGeminiClient(
        [
            success_response(
                result=model_output_sentinel,
                usage_metadata=_full_usage(),
            )
        ]
    )

    await runtime_type()(client=client).invoke(
        agent,
        "INPUT_OBJECT_SENTINEL_648a",
        attempt_number=2,
    )

    span = _one_runtime_span(capfire)
    attributes = dict(span.attributes or {})
    assert span.kind is SpanKind.CLIENT
    assert _application_attribute_keys(span) == {
        "agent_name",
        "attempt_number",
        "result",
    }
    assert attributes["agent_name"] == "trace_agent"
    assert attributes["attempt_number"] == 2
    assert attributes["result"] == "succeeded"
    assert attributes["gen_ai.operation.name"] == "generate_content"
    assert attributes["gen_ai.provider.name"] == "gcp.gemini"
    assert attributes["gen_ai.request.model"] == "gemini-trace-model"
    assert attributes["gen_ai.response.model"] == "gemini-trace-model"
    assert "prompt-version-sentinel-v1" not in _span_text(span)
    assert "SYSTEM_INSTRUCTIONS_SENTINEL_TRACE_e96b" not in _span_text(span)
    assert "TASK_CONTENTS_SENTINEL_TRACE_19a2" not in _span_text(span)
    assert "INPUT_OBJECT_SENTINEL_648a" not in _span_text(span)
    assert model_output_sentinel not in _span_text(span)
    assert "run_id" not in attributes


async def test_success_span_records_each_present_usage_field_once(
    capfire: CaptureLogfire,
) -> None:
    client = FakeGeminiClient([success_response(usage_metadata=_full_usage())])

    await runtime_type()(client=client).invoke(
        make_agent(),
        "typed input",
        attempt_number=1,
    )

    attributes = dict(_one_runtime_span(capfire).attributes or {})
    assert attributes["gen_ai.usage.input_tokens"] == 11
    assert attributes["gen_ai.usage.output_tokens"] == 7
    assert attributes["gen_ai.usage.cache_read.input_tokens"] == 3
    assert attributes["gen_ai.usage.reasoning.output_tokens"] == 2
    assert "gen_ai.usage.total_tokens" not in attributes
    assert not any("latency" in key for key in attributes)


async def test_missing_usage_fields_are_not_zero_filled(
    capfire: CaptureLogfire,
) -> None:
    usage = SimpleNamespace(
        prompt_token_count=13,
        candidates_token_count=None,
        cached_content_token_count=None,
        thoughts_token_count=None,
        total_token_count=13,
    )
    client = FakeGeminiClient([success_response(usage_metadata=usage)])

    await runtime_type()(client=client).invoke(
        make_agent(),
        "typed input",
        attempt_number=1,
    )

    attributes = dict(_one_runtime_span(capfire).attributes or {})
    assert attributes["gen_ai.usage.input_tokens"] == 13
    assert "gen_ai.usage.output_tokens" not in attributes
    assert "gen_ai.usage.cache_read.input_tokens" not in attributes
    assert "gen_ai.usage.reasoning.output_tokens" not in attributes


async def test_blocked_response_records_usage_and_classified_error_span(
    capfire: CaptureLogfire,
) -> None:
    client = FakeGeminiClient(
        [blocked_response("SAFETY", usage_metadata=_full_usage())]
    )

    with pytest.raises(AIProviderOutputBlockedError):
        await runtime_type()(client=client).invoke(
            make_agent(),
            "typed input",
            attempt_number=1,
        )

    span = _one_runtime_span(capfire)
    attributes = dict(span.attributes or {})
    assert attributes["result"] == "blocked"
    assert attributes["gen_ai.usage.input_tokens"] == 11
    assert attributes["gen_ai.usage.output_tokens"] == 7
    assert attributes["gen_ai.usage.cache_read.input_tokens"] == 3
    assert attributes["gen_ai.usage.reasoning.output_tokens"] == 2
    assert isinstance(attributes["error.type"], str)
    assert span.status.status_code is StatusCode.ERROR
    assert span.status.description in (None, "")
    assert _exception_events(span) == []
    assert _application_attribute_keys(span) == {
        "agent_name",
        "attempt_number",
        "result",
    }
    _assert_no_model_visible_text(span, "MODEL_OUTPUT_SENTINEL_BLOCKED_31d9")


async def test_invalid_response_records_usage_before_classification(
    capfire: CaptureLogfire,
) -> None:
    contract_module = runtime_contract()
    error_type = required_attribute(contract_module, "AgentResponseInvalidError")
    client = FakeGeminiClient(
        [
            FakeResponse(
                text="MODEL_OUTPUT_SENTINEL_INVALID_JSON_041b",
                usage_metadata=_full_usage(),
            )
        ]
    )

    with pytest.raises(error_type):
        await runtime_type()(client=client).invoke(
            make_agent(),
            "typed input",
            attempt_number=1,
        )

    span = _one_runtime_span(capfire)
    attributes = dict(span.attributes or {})
    assert attributes["result"] == "invalid_response"
    assert attributes["gen_ai.usage.input_tokens"] == 11
    assert attributes["gen_ai.usage.output_tokens"] == 7
    assert attributes["gen_ai.usage.cache_read.input_tokens"] == 3
    assert attributes["gen_ai.usage.reasoning.output_tokens"] == 2
    assert isinstance(attributes["error.type"], str)
    assert span.status.status_code is StatusCode.ERROR
    assert span.status.description in (None, "")
    assert _exception_events(span) == []
    assert _application_attribute_keys(span) == {
        "agent_name",
        "attempt_number",
        "result",
    }
    _assert_no_model_visible_text(span, "MODEL_OUTPUT_SENTINEL_INVALID_JSON_041b")


async def test_output_schema_mismatch_records_usage_and_safe_classified_span(
    capfire: CaptureLogfire,
) -> None:
    contract_module = runtime_contract()
    error_type = required_attribute(contract_module, "AgentResponseInvalidError")
    defect_type = required_attribute(contract_module, "AgentResponseDefect")
    model_output_sentinel = "MODEL_OUTPUT_SENTINEL_SCHEMA_MISMATCH_294c"
    payload = {
        "score": 0,
        "secret_number": model_output_sentinel,
        "unsafe": "trigger validator",
    }
    client = FakeGeminiClient(
        [
            FakeResponse(
                text=json.dumps(payload),
                usage_metadata=_full_usage(),
            )
        ]
    )

    with pytest.raises(error_type) as exc_info:
        await runtime_type()(client=client).invoke(
            make_agent(output_type=ValidationProbeOutput),
            "typed input",
            attempt_number=1,
        )

    span = _one_runtime_span(capfire)
    attributes = dict(span.attributes or {})
    span_text = _span_text(span)
    assert exc_info.value.defect is defect_type.OUTPUT_SCHEMA_MISMATCH
    assert attributes["result"] == "invalid_response"
    assert isinstance(attributes["error.type"], str)
    assert attributes["gen_ai.usage.input_tokens"] == 11
    assert attributes["gen_ai.usage.output_tokens"] == 7
    assert attributes["gen_ai.usage.cache_read.input_tokens"] == 3
    assert attributes["gen_ai.usage.reasoning.output_tokens"] == 2
    assert span.status.status_code is StatusCode.ERROR
    assert span.status.description in (None, "")
    assert _exception_events(span) == []
    assert _application_attribute_keys(span) == {
        "agent_name",
        "attempt_number",
        "result",
    }
    _assert_no_model_visible_text(
        span,
        model_output_sentinel,
        "ARBITRARY_CTX_SENTINEL_7c62",
    )
    assert "Input should be" not in span_text
    assert "errors.pydantic.dev" not in span_text


async def test_classified_provider_error_has_no_usage_or_exception_event(
    capfire: CaptureLogfire,
) -> None:
    client = FakeGeminiClient([TimeoutError("PROVIDER_ERROR_SENTINEL_267e")])

    with pytest.raises(AIProviderNetworkError):
        await runtime_type()(client=client).invoke(
            make_agent(),
            "typed input",
            attempt_number=1,
        )

    span = _one_runtime_span(capfire)
    attributes = dict(span.attributes or {})
    assert attributes["result"] == "provider_error"
    assert isinstance(attributes["error.type"], str)
    assert not any(key.startswith("gen_ai.usage.") for key in attributes)
    assert span.status.status_code is StatusCode.ERROR
    assert span.status.description in (None, "")
    assert _exception_events(span) == []
    assert _application_attribute_keys(span) == {
        "agent_name",
        "attempt_number",
        "result",
    }
    _assert_no_model_visible_text(span, "PROVIDER_ERROR_SENTINEL_267e")


async def test_unclassified_error_keeps_redacted_exception_event_without_result(
    capfire: CaptureLogfire,
) -> None:
    install_exception_redaction()
    error = RuntimeError("UNCLASSIFIED_EXCEPTION_SENTINEL_a17f")
    client = FakeGeminiClient([error])

    with pytest.raises(RuntimeError) as exc_info:
        await runtime_type()(client=client).invoke(
            make_agent(),
            "typed input",
            attempt_number=1,
        )

    span = _one_runtime_span(capfire)
    attributes = dict(span.attributes or {})
    events = _exception_events(span)
    assert exc_info.value is error
    assert "result" not in attributes
    assert len(events) == 1
    assert str(events[0].attributes["exception.type"]).endswith("RuntimeError")
    assert events[0].attributes["exception.message"] == "[redacted]"
    assert events[0].attributes["exception.stacktrace"] == "[redacted]"
    assert span.status.status_code is StatusCode.ERROR
    assert span.status.description == "[redacted]"
    assert _application_attribute_keys(span) == {"agent_name", "attempt_number"}
    _assert_no_model_visible_text(span, "UNCLASSIFIED_EXCEPTION_SENTINEL_a17f")


async def test_sequential_invokes_do_not_carry_usage_between_spans(
    capfire: CaptureLogfire,
) -> None:
    client = FakeGeminiClient(
        [
            success_response(result="first", usage_metadata=_full_usage()),
            success_response(result="second", usage_metadata=None),
        ]
    )
    runtime = runtime_type()(client=client)

    await runtime.invoke(
        make_agent(name="first_agent", model_name="first-model"),
        "first input",
        attempt_number=1,
    )
    await runtime.invoke(
        make_agent(name="second_agent", model_name="second-model"),
        "second input",
        attempt_number=2,
    )

    spans = _runtime_spans(capfire)
    assert len(spans) == 2
    assert (spans[0].attributes or {})["gen_ai.usage.input_tokens"] == 11
    assert not any(
        key.startswith("gen_ai.usage.") for key in (spans[1].attributes or {})
    )
    assert (spans[0].attributes or {})["agent_name"] == "first_agent"
    assert (spans[1].attributes or {})["agent_name"] == "second_agent"
    assert (spans[0].attributes or {})["gen_ai.request.model"] == "first-model"
    assert (spans[1].attributes or {})["gen_ai.request.model"] == "second-model"


async def test_renderer_failure_creates_no_provider_span(
    capfire: CaptureLogfire,
) -> None:
    error = RuntimeError("RENDERER_FAILURE_SENTINEL_7a1d")
    client = FakeGeminiClient([success_response()])
    agent = make_agent()
    agent = replace(
        agent,
        prompt=type(agent.prompt)(
            version=agent.prompt.version,
            instructions=agent.prompt.instructions,
            input_renderer=lambda _input: (_ for _ in ()).throw(error),
        ),
    )

    with pytest.raises(RuntimeError):
        await runtime_type()(client=client).invoke(
            agent,
            "typed input",
            attempt_number=1,
        )

    assert _runtime_spans(capfire) == []
    client.models.generate_content.assert_not_awaited()


async def test_config_failure_creates_no_provider_span(
    capfire: CaptureLogfire,
) -> None:
    client = FakeGeminiClient([success_response()])
    agent = make_agent()
    agent = replace(
        agent,
        model_settings=type(agent.model_settings)(
            temperature=SimpleNamespace(invalid="temperature"),
            max_output_tokens=321,
        ),
    )

    with pytest.raises((TypeError, ValueError)):
        await runtime_type()(client=client).invoke(
            agent,
            "typed input",
            attempt_number=1,
        )

    assert _runtime_spans(capfire) == []
    client.models.generate_content.assert_not_awaited()


async def test_non_gemini_agent_is_rejected_before_renderer_config_and_span(
    capfire: CaptureLogfire,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_module = required_module("app.agent.runtime.gemini")
    gemini_runtime_type = required_attribute(runtime_module, "GeminiAgentRuntime")
    config_factory = MagicMock(
        side_effect=AssertionError("config must not be constructed")
    )
    monkeypatch.setattr(runtime_module, "GenerateContentConfig", config_factory)
    renderer = MagicMock(return_value="contents must not be rendered")
    client = FakeGeminiClient([success_response()])
    agent = make_agent()
    agent = replace(
        agent,
        model=type(agent.model)(provider="openai", name="non-gemini-model"),
        prompt=type(agent.prompt)(
            version=agent.prompt.version,
            instructions=agent.prompt.instructions,
            input_renderer=renderer,
        ),
    )

    with pytest.raises(ValueError):
        await gemini_runtime_type(client=client).invoke(
            agent,
            "typed input",
            attempt_number=1,
        )

    renderer.assert_not_called()
    config_factory.assert_not_called()
    assert _runtime_spans(capfire) == []
    client.models.generate_content.assert_not_awaited()


@pytest.mark.parametrize("attempt_number", [0, -1])
async def test_invalid_attempt_number_creates_no_provider_span(
    capfire: CaptureLogfire,
    attempt_number: int,
) -> None:
    client = FakeGeminiClient([success_response()])

    with pytest.raises(ValueError):
        await runtime_type()(client=client).invoke(
            make_agent(),
            "typed input",
            attempt_number=attempt_number,
        )

    assert _runtime_spans(capfire) == []
    client.models.generate_content.assert_not_awaited()
