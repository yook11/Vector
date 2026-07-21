"""Streaming Agent Runtime のprovider-neutral契約。"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from enum import StrEnum
from inspect import signature
from types import SimpleNamespace
from typing import Any, cast

import pytest
from google.genai.client import AsyncClient
from opentelemetry import context as otel_context
from opentelemetry import trace
from opentelemetry.trace import (
    NonRecordingSpan,
    SpanContext,
    StatusCode,
    TraceFlags,
    TraceState,
)

import app.agent.runtime.gemini as gemini_runtime_module
import app.analysis.ai_provider_errors as ai_provider_errors
from app.agent.runtime.gemini import GeminiAgentRuntime
from app.analysis.ai_provider_errors import (
    AIProviderInputRejectedError,
    AIProviderNetworkError,
    AIProviderOutputBlockedError,
)
from tests.agent.runtime._deepseek_helpers import (
    FakeDeepSeekClient,
    make_binding,
)
from tests.agent.runtime._deepseek_helpers import (
    make_agent as make_deepseek_agent,
)
from tests.agent.runtime._deepseek_helpers import (
    runtime_type as deepseek_runtime_type,
)
from tests.agent.runtime._deepseek_helpers import (
    success_response as deepseek_success_response,
)
from tests.agent.runtime._helpers import FakeGeminiClient, make_agent, success_response


def _content_rejection_kind_type() -> type[StrEnum]:
    kind_type = getattr(
        ai_provider_errors,
        "AIProviderContentRejectionKind",
        None,
    )
    assert isinstance(kind_type, type) and issubclass(kind_type, StrEnum)
    return kind_type


class FakeSdkStream:
    def __init__(
        self,
        chunks: list[object | BaseException],
        *,
        close_error: BaseException | None = None,
        lifecycle_events: list[str] | None = None,
    ) -> None:
        self._chunks = iter(chunks)
        self._close_error = close_error
        self._lifecycle_events = lifecycle_events
        self.close_calls = 0

    def __aiter__(self) -> FakeSdkStream:
        return self

    async def __anext__(self) -> object:
        try:
            chunk = next(self._chunks)
        except StopIteration as exc:
            raise StopAsyncIteration from exc
        if isinstance(chunk, BaseException):
            raise chunk
        return chunk

    async def aclose(self) -> None:
        self.close_calls += 1
        if self._lifecycle_events is not None:
            self._lifecycle_events.append("sdk_close")
        if self._close_error is not None:
            raise self._close_error


class BlockingSdkStream(FakeSdkStream):
    def __init__(self) -> None:
        super().__init__([])
        self.next_started = asyncio.Event()
        self.release_next = asyncio.Event()

    async def __anext__(self) -> object:
        self.next_started.set()
        await self.release_next.wait()
        raise StopAsyncIteration


class FakeAttemptSpan:
    def __init__(
        self,
        attributes: dict[str, Any] | None,
        lifecycle_events: list[str] | None,
    ) -> None:
        self.attributes = dict(attributes or {})
        self.end_calls = 0
        self.exception_events: list[BaseException] = []
        self._lifecycle_events = lifecycle_events
        self.status_code = StatusCode.UNSET
        self.status_description: str | None = None

    def set_attribute(self, key: str, value: Any) -> None:
        self.attributes[key] = value

    def set_status(
        self,
        status_code: StatusCode,
        description: str | None = None,
    ) -> None:
        self.status_code = status_code
        self.status_description = description

    def record_exception(self, error: BaseException) -> None:
        self.exception_events.append(error)

    def end(self) -> None:
        self.end_calls += 1
        if self._lifecycle_events is not None:
            self._lifecycle_events.append("span_end")


class FakeTracer:
    def __init__(self, *, lifecycle_events: list[str] | None = None) -> None:
        self._lifecycle_events = lifecycle_events
        self.spans: list[FakeAttemptSpan] = []
        self.start_contexts: list[otel_context.Context | None] = []

    def start_span(
        self,
        _name: str,
        *,
        context: otel_context.Context | None = None,
        attributes: dict[str, Any] | None = None,
        **_kwargs: Any,
    ) -> FakeAttemptSpan:
        span = FakeAttemptSpan(attributes, self._lifecycle_events)
        self.spans.append(span)
        self.start_contexts.append(context)
        return span


def _terminal_chunk(text: str = "fragment") -> object:
    return _stream_chunk(text=text, finish_reason="STOP")


def _stream_chunk(
    *,
    text: str | None = "fragment",
    finish_reason: str | None = None,
    prompt_blocked: bool = False,
    usage_metadata: object | None = None,
) -> object:
    candidates = (
        []
        if finish_reason is None
        else [
            SimpleNamespace(
                finish_reason=SimpleNamespace(name=finish_reason),
            )
        ]
    )
    return SimpleNamespace(
        text=text,
        prompt_feedback=(
            SimpleNamespace(block_reason="BLOCKED") if prompt_blocked else None
        ),
        candidates=candidates,
        usage_metadata=usage_metadata,
    )


def _usage() -> object:
    return SimpleNamespace(
        prompt_token_count=11,
        candidates_token_count=7,
        cached_content_token_count=3,
        thoughts_token_count=2,
    )


def _phase_span(trace_id: int, span_id: int) -> NonRecordingSpan:
    return NonRecordingSpan(
        SpanContext(
            trace_id=trace_id,
            span_id=span_id,
            is_remote=False,
            trace_flags=TraceFlags.SAMPLED,
            trace_state=TraceState(),
        )
    )


def test_streaming_contract_is_separate_from_non_streaming_runtime() -> None:
    from app.agent.runtime.contract import (
        AgentRuntime,
        AgentTextStream,
        StreamingAgentRuntime,
        StreamingAgentRuntimeScopeFactory,
    )

    assert list(signature(AgentRuntime.invoke).parameters) == [
        "self",
        "agent",
        "input",
        "attempt_number",
    ]
    assert list(signature(StreamingAgentRuntime.invoke_stream).parameters) == [
        "self",
        "agent",
        "input",
        "attempt_number",
    ]
    assert getattr(AgentTextStream, "_is_protocol", False)
    assert getattr(StreamingAgentRuntime, "_is_protocol", False)
    assert (
        signature(StreamingAgentRuntime.invoke_stream)
        .parameters["attempt_number"]
        .kind.name
        == "KEYWORD_ONLY"
    )
    assert list(signature(AgentTextStream.__anext__).parameters) == ["self"]
    assert list(signature(AgentTextStream.aclose).parameters) == ["self"]
    assert list(signature(StreamingAgentRuntimeScopeFactory.__call__).parameters) == [
        "self"
    ]
    assert getattr(StreamingAgentRuntimeScopeFactory, "_is_protocol", False)
    assert not hasattr(deepseek_runtime_type(), "invoke_stream")


async def test_unstarted_stream_close_does_not_open_provider_stream_or_span(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sdk_stream = FakeSdkStream([_terminal_chunk()])
    client = FakeGeminiClient([], streams=[sdk_stream])
    runtime = GeminiAgentRuntime(client=cast(AsyncClient, client))
    tracer = FakeTracer()
    monkeypatch.setattr(gemini_runtime_module, "_TRACER", tracer)

    stream = runtime.invoke_stream(
        make_agent(response_schema=None),
        "typed input",
        attempt_number=1,
    )
    await stream.aclose()

    client.models.generate_content_stream.assert_not_awaited()
    assert sdk_stream.close_calls == 0
    assert tracer.spans == []


async def test_first_iteration_opens_one_stream_and_yields_fragments_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sdk_stream = FakeSdkStream([_terminal_chunk("MODEL_FRAGMENT_SENTINEL")])
    client = FakeGeminiClient([], streams=[sdk_stream])
    runtime = GeminiAgentRuntime(client=cast(AsyncClient, client))
    tracer = FakeTracer()
    monkeypatch.setattr(gemini_runtime_module, "_TRACER", tracer)

    stream = runtime.invoke_stream(
        make_agent(response_schema=None),
        "typed input",
        attempt_number=2,
    )
    fragments = [fragment async for fragment in stream]

    assert fragments == ["MODEL_FRAGMENT_SENTINEL"]
    assert client.models.generate_content_stream.await_count == 1
    assert sdk_stream.close_calls == 1
    assert tracer.spans[0].end_calls == 1
    assert tracer.spans[0].attributes["result"] == "succeeded"
    kwargs = client.models.generate_content_stream.await_args.kwargs
    assert kwargs["contents"] == "TASK_CONTENTS_SENTINEL_8a43"
    assert kwargs["config"].system_instruction == "SYSTEM_INSTRUCTIONS_SENTINEL_5f21"
    explicit_config = kwargs["config"].model_dump(exclude_unset=True)
    assert "response_mime_type" not in explicit_config
    assert "response_schema" not in explicit_config


async def test_structured_stream_request_keeps_declared_response_schema(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tracer = FakeTracer()
    monkeypatch.setattr(gemini_runtime_module, "_TRACER", tracer)
    sdk_stream = FakeSdkStream([_terminal_chunk()])
    client = FakeGeminiClient([], streams=[sdk_stream])

    fragments = [
        fragment
        async for fragment in GeminiAgentRuntime(
            client=cast(AsyncClient, client)
        ).invoke_stream(make_agent(), "typed input", attempt_number=1)
    ]

    config = client.models.generate_content_stream.await_args.kwargs["config"]
    assert fragments == ["fragment"]
    assert config.response_mime_type == "application/json"
    assert config.response_schema["type"] == "OBJECT"


async def test_invoke_stream_captures_creation_phase_parent_without_becoming_ambient(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tracer = FakeTracer()
    monkeypatch.setattr(gemini_runtime_module, "_TRACER", tracer)
    sdk_stream = FakeSdkStream([_terminal_chunk("MODEL_FRAGMENT_SENTINEL")])
    runtime = GeminiAgentRuntime(
        client=cast(AsyncClient, FakeGeminiClient([], streams=[sdk_stream]))
    )
    creation_phase = _phase_span(1, 1)
    creation_token = otel_context.attach(trace.set_span_in_context(creation_phase))
    try:
        stream = runtime.invoke_stream(
            make_agent(response_schema=None),
            "typed input",
            attempt_number=1,
        )
    finally:
        otel_context.detach(creation_token)

    consumer_phase = _phase_span(2, 2)
    consumer_token = otel_context.attach(trace.set_span_in_context(consumer_phase))
    try:
        fragment = await stream.__anext__()
        current_span_after_yield = trace.get_current_span()
    finally:
        await stream.aclose()
        otel_context.detach(consumer_token)

    assert fragment == "MODEL_FRAGMENT_SENTINEL"
    assert trace.get_current_span(context=tracer.start_contexts[0]) is creation_phase
    assert current_span_after_yield is consumer_phase
    assert tracer.spans[0].end_calls == 1


async def test_invoke_stream_rejects_renderer_failure_before_iterator_span_and_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    error = RuntimeError("RENDERER_MUST_NOT_RUN_AFTER_FAILURE")
    agent = make_agent(response_schema=None)
    agent = replace(
        agent,
        prompt=replace(
            agent.prompt,
            input_renderer=lambda _input: (_ for _ in ()).throw(error),
        ),
    )
    client = FakeGeminiClient([])
    tracer = FakeTracer()
    monkeypatch.setattr(gemini_runtime_module, "_TRACER", tracer)

    with pytest.raises(RuntimeError) as exc_info:
        GeminiAgentRuntime(client=cast(AsyncClient, client)).invoke_stream(
            agent,
            "typed input",
            attempt_number=1,
        )

    assert exc_info.value is error
    client.models.generate_content_stream.assert_not_awaited()
    assert tracer.spans == []


async def test_invoke_stream_rejects_config_failure_before_iterator_span_and_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = replace(
        make_agent(response_schema=None),
        model_settings=SimpleNamespace(
            temperature=SimpleNamespace(invalid="temperature"),
            max_output_tokens=321,
        ),
    )
    client = FakeGeminiClient([])
    tracer = FakeTracer()
    monkeypatch.setattr(gemini_runtime_module, "_TRACER", tracer)

    with pytest.raises((TypeError, ValueError)):
        GeminiAgentRuntime(client=cast(AsyncClient, client)).invoke_stream(
            agent,
            "typed input",
            attempt_number=1,
        )

    client.models.generate_content_stream.assert_not_awaited()
    assert tracer.spans == []


async def test_prompt_block_records_usage_then_classified_error_and_closes_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tracer = FakeTracer()
    monkeypatch.setattr(gemini_runtime_module, "_TRACER", tracer)
    sdk_stream = FakeSdkStream(
        [
            _stream_chunk(
                text="MUST_NOT_YIELD",
                prompt_blocked=True,
                usage_metadata=_usage(),
            )
        ]
    )
    runtime = GeminiAgentRuntime(
        client=cast(AsyncClient, FakeGeminiClient([], streams=[sdk_stream]))
    )

    with pytest.raises(AIProviderInputRejectedError) as exc_info:
        _ = [
            fragment
            async for fragment in runtime.invoke_stream(
                make_agent(response_schema=None),
                "typed input",
                attempt_number=1,
            )
        ]

    assert exc_info.value.rejection_kind is (  # type: ignore[attr-defined]
        _content_rejection_kind_type().SAFETY  # type: ignore[attr-defined]
    )
    span = tracer.spans[0]
    assert span.attributes["result"] == "provider_error"
    assert span.attributes["gen_ai.usage.input_tokens"] == 11
    assert span.attributes["gen_ai.usage.output_tokens"] == 7
    assert span.status_code is StatusCode.ERROR
    assert span.status_description is None
    assert span.exception_events == []
    assert sdk_stream.close_calls == 1
    assert span.end_calls == 1


@pytest.mark.parametrize(
    ("finish_reason", "expected_kind_name"),
    [("SAFETY", "SAFETY"), ("RECITATION", "OTHER")],
)
async def test_blocked_finish_reason_records_blocked_outcome_without_event(
    monkeypatch: pytest.MonkeyPatch,
    finish_reason: str,
    expected_kind_name: str,
) -> None:
    tracer = FakeTracer()
    monkeypatch.setattr(gemini_runtime_module, "_TRACER", tracer)
    sdk_stream = FakeSdkStream([_stream_chunk(finish_reason=finish_reason)])
    runtime = GeminiAgentRuntime(
        client=cast(AsyncClient, FakeGeminiClient([], streams=[sdk_stream]))
    )

    with pytest.raises(AIProviderOutputBlockedError) as exc_info:
        _ = [
            fragment
            async for fragment in runtime.invoke_stream(
                make_agent(response_schema=None),
                "typed input",
                attempt_number=1,
            )
        ]

    assert exc_info.value.rejection_kind is getattr(  # type: ignore[attr-defined]
        _content_rejection_kind_type(),
        expected_kind_name,
    )
    span = tracer.spans[0]
    assert span.attributes["result"] == "blocked"
    assert span.status_code is StatusCode.ERROR
    assert span.exception_events == []
    assert sdk_stream.close_calls == 1
    assert span.end_calls == 1


async def test_stream_without_terminal_reason_is_truncated_and_closed_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tracer = FakeTracer()
    monkeypatch.setattr(gemini_runtime_module, "_TRACER", tracer)
    sdk_stream = FakeSdkStream([_stream_chunk(text="first fragment")])
    runtime = GeminiAgentRuntime(
        client=cast(AsyncClient, FakeGeminiClient([], streams=[sdk_stream]))
    )

    with pytest.raises(AIProviderNetworkError):
        _ = [
            fragment
            async for fragment in runtime.invoke_stream(
                make_agent(response_schema=None),
                "typed input",
                attempt_number=1,
            )
        ]

    span = tracer.spans[0]
    assert span.attributes["result"] == "provider_error"
    assert span.status_code is StatusCode.ERROR
    assert sdk_stream.close_calls == 1
    assert span.end_calls == 1


async def test_translated_provider_error_preserves_cause_after_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tracer = FakeTracer()
    monkeypatch.setattr(gemini_runtime_module, "_TRACER", tracer)
    source_error = TimeoutError("PROVIDER_TIMEOUT_SENTINEL")
    client = FakeGeminiClient([], streams=[source_error])
    runtime = GeminiAgentRuntime(client=cast(AsyncClient, client))

    with pytest.raises(AIProviderNetworkError) as exc_info:
        await runtime.invoke_stream(
            make_agent(response_schema=None),
            "typed input",
            attempt_number=1,
        ).__anext__()

    span = tracer.spans[0]
    assert exc_info.value.__cause__ is source_error
    assert span.attributes["result"] == "provider_error"
    assert span.exception_events == []
    assert span.end_calls == 1


async def test_unclassified_provider_error_records_one_event_then_closes_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lifecycle_events: list[str] = []
    tracer = FakeTracer(lifecycle_events=lifecycle_events)
    monkeypatch.setattr(gemini_runtime_module, "_TRACER", tracer)
    error = RuntimeError("UNCLASSIFIED_PROVIDER_SENTINEL")
    sdk_stream = FakeSdkStream([error], lifecycle_events=lifecycle_events)
    runtime = GeminiAgentRuntime(
        client=cast(AsyncClient, FakeGeminiClient([], streams=[sdk_stream]))
    )

    with pytest.raises(RuntimeError) as exc_info:
        await runtime.invoke_stream(
            make_agent(response_schema=None),
            "typed input",
            attempt_number=1,
        ).__anext__()

    span = tracer.spans[0]
    assert exc_info.value is error
    assert "result" not in span.attributes
    assert span.status_code is StatusCode.ERROR
    assert span.exception_events == [error]
    assert sdk_stream.close_calls == 1
    assert span.end_calls == 1
    assert lifecycle_events == ["sdk_close", "span_end"]


async def test_consumer_aclose_is_abandonment_without_error_outcome_and_ends_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tracer = FakeTracer()
    monkeypatch.setattr(gemini_runtime_module, "_TRACER", tracer)
    sdk_stream = FakeSdkStream([_stream_chunk(text="fragment")])
    runtime = GeminiAgentRuntime(
        client=cast(AsyncClient, FakeGeminiClient([], streams=[sdk_stream]))
    )
    stream = runtime.invoke_stream(
        make_agent(response_schema=None),
        "typed input",
        attempt_number=1,
    )

    assert await stream.__anext__() == "fragment"
    await stream.aclose()
    await stream.aclose()

    span = tracer.spans[0]
    assert "result" not in span.attributes
    assert "error.type" not in span.attributes
    assert span.status_code is StatusCode.UNSET
    assert span.exception_events == []
    assert sdk_stream.close_calls == 1
    assert span.end_calls == 1


async def test_usage_before_fragment_yield_survives_consumer_abandonment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tracer = FakeTracer()
    monkeypatch.setattr(gemini_runtime_module, "_TRACER", tracer)
    sdk_stream = FakeSdkStream(
        [_stream_chunk(text="fragment", usage_metadata=_usage())]
    )
    runtime = GeminiAgentRuntime(
        client=cast(AsyncClient, FakeGeminiClient([], streams=[sdk_stream]))
    )
    stream = runtime.invoke_stream(
        make_agent(response_schema=None),
        "typed input",
        attempt_number=1,
    )

    assert await stream.__anext__() == "fragment"

    span = tracer.spans[0]
    assert span.attributes["gen_ai.usage.input_tokens"] == 11
    assert span.attributes["gen_ai.usage.output_tokens"] == 7

    await stream.aclose()

    assert span.attributes["gen_ai.usage.input_tokens"] == 11
    assert span.attributes["gen_ai.usage.output_tokens"] == 7
    assert "result" not in span.attributes
    assert "error.type" not in span.attributes
    assert span.status_code is StatusCode.UNSET
    assert span.exception_events == []
    assert sdk_stream.close_calls == 1
    assert span.end_calls == 1


async def test_cancellation_during_provider_next_closes_span_once_without_outcome(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tracer = FakeTracer()
    monkeypatch.setattr(gemini_runtime_module, "_TRACER", tracer)
    sdk_stream = BlockingSdkStream()
    runtime = GeminiAgentRuntime(
        client=cast(AsyncClient, FakeGeminiClient([], streams=[sdk_stream]))
    )
    stream = runtime.invoke_stream(
        make_agent(response_schema=None),
        "typed input",
        attempt_number=1,
    )
    next_task = asyncio.create_task(stream.__anext__())
    await sdk_stream.next_started.wait()
    next_task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await next_task

    span = tracer.spans[0]
    assert "result" not in span.attributes
    assert "error.type" not in span.attributes
    assert span.status_code is StatusCode.UNSET
    assert span.exception_events == []
    assert sdk_stream.close_calls == 1
    assert span.end_calls == 1


async def test_cancellation_while_consumer_handles_fragment_closes_once_without_outcome(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tracer = FakeTracer()
    monkeypatch.setattr(gemini_runtime_module, "_TRACER", tracer)
    sdk_stream = FakeSdkStream([_stream_chunk(text="fragment")])
    runtime = GeminiAgentRuntime(
        client=cast(AsyncClient, FakeGeminiClient([], streams=[sdk_stream]))
    )
    stream = runtime.invoke_stream(
        make_agent(response_schema=None),
        "typed input",
        attempt_number=1,
    )
    consumer_started = asyncio.Event()
    wait_for_cancellation = asyncio.Event()

    async def consume_fragment_then_close() -> None:
        try:
            assert await stream.__anext__() == "fragment"
            consumer_started.set()
            await wait_for_cancellation.wait()
        finally:
            await stream.aclose()

    consumer_task = asyncio.create_task(consume_fragment_then_close())
    await consumer_started.wait()
    consumer_task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await consumer_task

    span = tracer.spans[0]
    assert "result" not in span.attributes
    assert "error.type" not in span.attributes
    assert span.status_code is StatusCode.UNSET
    assert span.exception_events == []
    assert sdk_stream.close_calls == 1
    assert span.end_calls == 1


async def test_sdk_close_error_is_best_effort_but_span_ends_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tracer = FakeTracer()
    monkeypatch.setattr(gemini_runtime_module, "_TRACER", tracer)
    sdk_stream = FakeSdkStream(
        [_terminal_chunk()],
        close_error=RuntimeError("SDK_CLOSE_FAILURE"),
    )
    runtime = GeminiAgentRuntime(
        client=cast(AsyncClient, FakeGeminiClient([], streams=[sdk_stream]))
    )

    fragments = [
        fragment
        async for fragment in runtime.invoke_stream(
            make_agent(response_schema=None),
            "typed input",
            attempt_number=1,
        )
    ]

    span = tracer.spans[0]
    assert fragments == ["fragment"]
    assert span.attributes["result"] == "succeeded"
    assert sdk_stream.close_calls == 1
    assert span.end_calls == 1


async def test_cancelled_sdk_close_preserves_usage_and_ends_span_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tracer = FakeTracer()
    monkeypatch.setattr(gemini_runtime_module, "_TRACER", tracer)
    sdk_stream = FakeSdkStream(
        [_stream_chunk(finish_reason="STOP", usage_metadata=_usage())],
        close_error=asyncio.CancelledError(),
    )
    runtime = GeminiAgentRuntime(
        client=cast(AsyncClient, FakeGeminiClient([], streams=[sdk_stream]))
    )
    stream = runtime.invoke_stream(
        make_agent(response_schema=None),
        "typed input",
        attempt_number=1,
    )

    assert await stream.__anext__() == "fragment"
    with pytest.raises(asyncio.CancelledError):
        await stream.__anext__()

    span = tracer.spans[0]
    assert span.attributes["gen_ai.usage.input_tokens"] == 11
    assert "result" not in span.attributes
    assert span.status_code is StatusCode.UNSET
    assert sdk_stream.close_calls == 1
    assert span.end_calls == 1


@pytest.mark.parametrize("runtime_name", ["gemini", "deepseek"])
async def test_non_streaming_runtime_rejects_schema_none_before_renderer_and_provider(
    runtime_name: str,
) -> None:
    renderer_error = RuntimeError("RENDERER_MUST_NOT_RUN")
    if runtime_name == "gemini":
        client = FakeGeminiClient([success_response()])
        runtime: Any = GeminiAgentRuntime(client=cast(AsyncClient, client))
        agent = make_agent(response_schema=None)
        provider_call = client.models.generate_content
    else:
        client = FakeDeepSeekClient([deepseek_success_response()])
        runtime = deepseek_runtime_type()(client=client, binding=make_binding())
        agent = replace(make_deepseek_agent(), response_schema=None)
        provider_call = client.chat.completions.create
    assert agent.response_schema is None
    agent = replace(
        agent,
        prompt=replace(
            agent.prompt,
            input_renderer=lambda _input: (_ for _ in ()).throw(renderer_error),
        ),
    )

    with pytest.raises(ValueError, match="response_schema"):
        await runtime.invoke(agent, object(), attempt_number=1)

    provider_call.assert_not_awaited()
