"""DeepSeekAgentRuntime provider-attempt span 契約。

SDK I/O だけを fake にし、span 生成と結果分類は production Runtime を通す。
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import cast

import pytest
from logfire.testing import CaptureLogfire
from openai import AsyncOpenAI
from opentelemetry.trace import SpanKind, StatusCode

from app.agent.runtime.contract import AgentResponseInvalidError
from app.agent.runtime.deepseek import DeepSeekAgentRuntime
from app.analysis.ai_provider_errors import AIProviderNetworkError
from app.logfire.redaction import install_exception_redaction
from tests.agent.runtime._deepseek_helpers import (
    FakeDeepSeekClient,
    function_response,
    make_agent,
    make_binding,
    success_response,
)
from tests.agent.runtime._tracing_helpers import (
    application_attribute_keys,
    exception_events,
    one_provider_attempt_span,
    provider_attempt_spans,
    span_text,
)


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
    """成功 span が許可属性だけを持ち入出力本文を露出しない。"""
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

    await DeepSeekAgentRuntime(
        client=cast(AsyncOpenAI, client), binding=make_binding()
    ).invoke(agent, object(), attempt_number=2)

    span = one_provider_attempt_span(capfire)
    attributes = dict(span.attributes or {})
    assert span.kind is SpanKind.CLIENT
    assert application_attribute_keys(span) == {
        "agent_name",
        "attempt_number",
        "prompt_version",
        "result",
    }
    assert attributes["agent_name"] == "deepseek_trace_agent"
    assert attributes["attempt_number"] == 2
    assert attributes["result"] == "succeeded"
    assert attributes["gen_ai.operation.name"] == "chat"
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
        model_output_sentinel,
        response_model_sentinel,
    ):
        assert sentinel not in span_text(span)


async def test_provider_attempt_span_records_prompt_version(
    capfire: CaptureLogfire,
) -> None:
    """provider attempt span が prompt version を必ず記録する。"""
    client = FakeDeepSeekClient([success_response()])

    await DeepSeekAgentRuntime(
        client=cast(AsyncOpenAI, client), binding=make_binding()
    ).invoke(make_agent(), object(), attempt_number=1)

    attributes = dict(one_provider_attempt_span(capfire).attributes or {})
    assert attributes.get("prompt_version") == "PROMPT_VERSION_SENTINEL_v1"


async def test_invalid_response_records_usage_before_safe_classification(
    capfire: CaptureLogfire,
) -> None:
    """不正応答でも使用量を安全な分類結果より先に残す。"""
    client = FakeDeepSeekClient(
        [
            function_response(
                arguments="MODEL_OUTPUT_INVALID_JSON_SENTINEL_041b",
                usage=_usage(),
            )
        ]
    )

    with pytest.raises(AgentResponseInvalidError):
        await DeepSeekAgentRuntime(
            client=cast(AsyncOpenAI, client), binding=make_binding()
        ).invoke(make_agent(), object(), attempt_number=1)

    span = one_provider_attempt_span(capfire)
    attributes = dict(span.attributes or {})
    assert attributes["result"] == "invalid_response"
    assert isinstance(attributes["error.type"], str)
    assert attributes["gen_ai.usage.input_tokens"] == 11
    assert attributes["gen_ai.usage.output_tokens"] == 7
    assert attributes["gen_ai.usage.cache_read.input_tokens"] == 3
    assert attributes["gen_ai.usage.reasoning.output_tokens"] == 2
    assert span.status.status_code is StatusCode.ERROR
    assert span.status.description in (None, "")
    assert exception_events(span) == []
    assert "MODEL_OUTPUT_INVALID_JSON_SENTINEL_041b" not in span_text(span)


async def test_schema_mismatch_span_does_not_record_model_output_or_exception_event(
    capfire: CaptureLogfire,
) -> None:
    """schema mismatch span にモデル本文と例外 event を含めない。"""
    sentinel = "MODEL_OUTPUT_SCHEMA_SENTINEL_0bea"
    client = FakeDeepSeekClient(
        [
            function_response(
                arguments=json.dumps({"result": sentinel, "unexpected": sentinel}),
                usage=_usage(),
            )
        ]
    )

    with pytest.raises(AgentResponseInvalidError):
        await DeepSeekAgentRuntime(
            client=cast(AsyncOpenAI, client), binding=make_binding()
        ).invoke(make_agent(), object(), attempt_number=1)

    span = one_provider_attempt_span(capfire)
    assert exception_events(span) == []
    assert span.status.description in (None, "")
    assert sentinel not in span_text(span)


async def test_classified_provider_error_records_safe_span_without_exception_event(
    capfire: CaptureLogfire,
) -> None:
    """分類済み provider 障害を例外 event なしの安全な span として残す。"""
    sentinel = "PROVIDER_ERROR_SENTINEL_267e"
    client = FakeDeepSeekClient([TimeoutError(sentinel)])

    with pytest.raises(AIProviderNetworkError):
        await DeepSeekAgentRuntime(
            client=cast(AsyncOpenAI, client), binding=make_binding()
        ).invoke(make_agent(), object(), attempt_number=1)

    span = one_provider_attempt_span(capfire)
    attributes = dict(span.attributes or {})
    assert attributes["result"] == "provider_error"
    assert attributes["error.type"] == AIProviderNetworkError.CODE
    assert not any(key.startswith("gen_ai.usage.") for key in attributes)
    assert span.status.status_code is StatusCode.ERROR
    assert span.status.description in (None, "")
    assert exception_events(span) == []
    assert sentinel not in span_text(span)


async def test_unclassified_error_keeps_redacted_exception_event_without_result(
    capfire: CaptureLogfire,
) -> None:
    """未分類障害の span では例外 event を秘匿し結果を記録しない。"""
    install_exception_redaction()
    error = RuntimeError("UNCLASSIFIED_DEEPSEEK_SENTINEL_35ce")
    client = FakeDeepSeekClient([error])

    with pytest.raises(RuntimeError) as raised:
        await DeepSeekAgentRuntime(
            client=cast(AsyncOpenAI, client), binding=make_binding()
        ).invoke(make_agent(), object(), attempt_number=1)

    span = one_provider_attempt_span(capfire)
    attributes = dict(span.attributes or {})
    events = exception_events(span)
    assert raised.value is error
    assert "result" not in attributes
    assert len(events) == 1
    assert events[0].attributes["exception.message"] == "[redacted]"
    assert events[0].attributes["exception.stacktrace"] == "[redacted]"
    assert span.status.status_code is StatusCode.ERROR
    assert span.status.description == "[redacted]"
    assert "UNCLASSIFIED_DEEPSEEK_SENTINEL_35ce" not in span_text(span)


async def test_renderer_failure_creates_no_provider_span(
    capfire: CaptureLogfire,
) -> None:
    """入力描画で失敗した試行は provider span を生成しない。"""
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
        await DeepSeekAgentRuntime(
            client=cast(AsyncOpenAI, client), binding=make_binding()
        ).invoke(agent, object(), attempt_number=1)

    assert raised.value is error
    assert provider_attempt_spans(capfire) == []
    client.chat.completions.create.assert_not_awaited()
