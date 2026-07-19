"""Direct Answer phase とstreaming attemptのtrace境界。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import cast

import pytest
from google.genai.client import AsyncClient
from logfire.testing import CaptureLogfire
from opentelemetry.trace import StatusCode

from app.agent.answering.contract import AnsweringRequest
from app.agent.answering.direct_answer import flow as direct_answer_flow_module
from app.agent.answering.direct_answer.agent import DIRECT_ANSWER_AGENT
from app.agent.answering.direct_answer.contract import DirectAnswerInput
from app.agent.answering.direct_answer.flow import DirectAnswerFlow
from app.agent.contract import AnswerGenerationContinuation, AnswerGenerationStopped
from app.agent.question_context.contract import QuestionContext
from app.agent.runtime.contract import StreamingAgentRuntime
from app.agent.runtime.gemini import GeminiAgentRuntime
from tests.agent.runtime._helpers import FakeGeminiClient
from tests.agent.runtime._tracing_helpers import span_text


class _SdkStream:
    def __init__(self, text: str = "MODEL_ANSWER_SENTINEL") -> None:
        self._chunks = iter(
            [
                SimpleNamespace(
                    text=text,
                    prompt_feedback=None,
                    candidates=[
                        SimpleNamespace(finish_reason=SimpleNamespace(name="STOP"))
                    ],
                    usage_metadata=None,
                )
            ]
        )
        self.close_calls = 0

    def __aiter__(self) -> _SdkStream:
        return self

    async def __anext__(self) -> object:
        try:
            return next(self._chunks)
        except StopIteration as exc:
            raise StopAsyncIteration from exc

    async def aclose(self) -> None:
        self.close_calls += 1


def _request() -> AnsweringRequest:
    return AnsweringRequest(
        context=QuestionContext(
            standalone_question="MODEL_QUESTION_SENTINEL",
        ),
        as_of=datetime(2026, 7, 19, tzinfo=UTC),
    )


async def test_phase_owns_detached_streaming_attempt_without_model_text(
    capfire: CaptureLogfire,
) -> None:
    sdk_stream = _SdkStream()
    client = FakeGeminiClient([], streams=[sdk_stream])

    @asynccontextmanager
    async def runtime_scope() -> AsyncIterator[StreamingAgentRuntime]:
        yield GeminiAgentRuntime(client=cast(AsyncClient, client))

    draft = await DirectAnswerFlow(
        agent=DIRECT_ANSWER_AGENT,
        runtime_scope_factory=runtime_scope,
    ).answer(request=_request())

    spans = capfire.exporter.exported_spans
    phase_spans = [
        span
        for span in spans
        if span.name == "agent_phase"
        and (span.attributes or {}).get("logfire.span_type") == "span"
    ]
    attempt_spans = [
        span
        for span in spans
        if span.name == "agent_provider_call"
        and (span.attributes or {}).get("logfire.span_type") == "span"
    ]
    assert draft.answer == "MODEL_ANSWER_SENTINEL"
    assert len(phase_spans) == 1
    assert len(attempt_spans) == 1
    phase = phase_spans[0]
    attempt = attempt_spans[0]
    provider_request = client.models.generate_content_stream.await_args.kwargs
    provider_config = provider_request["config"]
    assert attempt.parent is not None
    assert attempt.parent.span_id == phase.context.span_id
    assert attempt.status.status_code is StatusCode.UNSET
    assert (attempt.attributes or {})["result"] == "succeeded"
    assert sdk_stream.close_calls == 1
    assert provider_config.system_instruction == DIRECT_ANSWER_AGENT.prompt.instructions
    assert provider_request["contents"] == DIRECT_ANSWER_AGENT.prompt.input_renderer(
        DirectAnswerInput(request=_request(), previous_answer="")
    )
    assert "MODEL_QUESTION_SENTINEL" not in provider_config.system_instruction
    assert "MODEL_ANSWER_SENTINEL" not in provider_request["contents"]
    observed = f"{span_text(phase)}\n{span_text(attempt)}"
    assert "MODEL_QUESTION_SENTINEL" not in observed
    assert "MODEL_ANSWER_SENTINEL" not in observed


async def test_retry_provider_request_adds_only_rendered_repair_context() -> None:
    first_stream = _SdkStream(" \n")
    second_stream = _SdkStream("再試行後の回答")
    client = FakeGeminiClient([], streams=[first_stream, second_stream])

    @asynccontextmanager
    async def runtime_scope() -> AsyncIterator[StreamingAgentRuntime]:
        yield GeminiAgentRuntime(client=cast(AsyncClient, client))

    draft = await DirectAnswerFlow(
        agent=DIRECT_ANSWER_AGENT,
        runtime_scope_factory=runtime_scope,
    ).answer(request=_request())

    requests = client.models.generate_content_stream.await_args_list
    first_contents = requests[0].kwargs["contents"]
    retry_contents = requests[1].kwargs["contents"]

    assert draft.answer == "再試行後の回答"
    assert len(requests) == 2
    assert "# Repair Context" not in first_contents
    assert "# Repair Context" in retry_contents
    assert "direct_answer_blank_response" in retry_contents
    assert all(
        request.kwargs["config"].system_instruction
        == DIRECT_ANSWER_AGENT.prompt.instructions
        for request in requests
    )
    assert (first_stream.close_calls, second_stream.close_calls) == (1, 1)


async def test_routine_stop_closes_phase_without_error_or_attempt(
    capfire: CaptureLogfire,
) -> None:
    class StopImmediately:
        async def should_continue(self) -> bool:
            return False

    class UnusedRuntime:
        def invoke_stream(self, *_args: object, **_kwargs: object) -> object:
            raise AssertionError("stream must not start")

    @asynccontextmanager
    async def runtime_scope() -> AsyncIterator[StreamingAgentRuntime]:
        yield cast(StreamingAgentRuntime, UnusedRuntime())

    flow = DirectAnswerFlow(
        agent=DIRECT_ANSWER_AGENT,
        runtime_scope_factory=runtime_scope,
        continuation=StopImmediately(),
    )
    try:
        await flow.answer(request=_request())
    except AnswerGenerationStopped as error:
        stopped = error
    else:
        raise AssertionError("routine stop must propagate")

    spans = capfire.exporter.exported_spans
    phase = next(
        span
        for span in spans
        if span.name == "agent_phase"
        and (span.attributes or {}).get("logfire.span_type") == "span"
    )
    attempt_spans = [
        span
        for span in spans
        if span.name == "agent_provider_call"
        and (span.attributes or {}).get("logfire.span_type") == "span"
    ]
    assert isinstance(stopped, AnswerGenerationStopped)
    assert phase.status.status_code is StatusCode.UNSET
    assert phase.events == ()
    assert attempt_spans == []


async def test_mid_stream_stop_abandons_real_attempt_without_error(
    capfire: CaptureLogfire,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sdk_stream = _SdkStream()
    client = FakeGeminiClient([], streams=[sdk_stream])
    stopped = AnswerGenerationStopped()
    continuation_checks = 0

    async def stop_after_stream_starts(_continuation: object) -> None:
        nonlocal continuation_checks
        continuation_checks += 1
        if continuation_checks == 2:
            raise stopped

    monkeypatch.setattr(
        direct_answer_flow_module,
        "ensure_answer_generation_continues",
        stop_after_stream_starts,
    )

    @asynccontextmanager
    async def runtime_scope() -> AsyncIterator[StreamingAgentRuntime]:
        yield GeminiAgentRuntime(client=cast(AsyncClient, client))

    flow = DirectAnswerFlow(
        agent=DIRECT_ANSWER_AGENT,
        runtime_scope_factory=runtime_scope,
        continuation=cast(AnswerGenerationContinuation, object()),
    )
    with pytest.raises(AnswerGenerationStopped) as exc_info:
        await flow.answer(request=_request())

    spans = capfire.exporter.exported_spans
    phase = next(
        span
        for span in spans
        if span.name == "agent_phase"
        and (span.attributes or {}).get("logfire.span_type") == "span"
    )
    attempt = next(
        span
        for span in spans
        if span.name == "agent_provider_call"
        and (span.attributes or {}).get("logfire.span_type") == "span"
    )
    attempt_attributes = attempt.attributes or {}

    assert exc_info.value is stopped
    assert continuation_checks == 2
    assert sdk_stream.close_calls == 1
    assert phase.status.status_code is StatusCode.UNSET
    assert phase.events == ()
    assert attempt.status.status_code is StatusCode.UNSET
    assert attempt.events == ()
    assert "result" not in attempt_attributes
    assert "error.type" not in attempt_attributes
