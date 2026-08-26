"""Gemini stream の LLM call 記録契約。"""

from __future__ import annotations

import asyncio
from typing import cast

import pytest
from google.genai.client import AsyncClient

from app.agent.recording.types import LlmCallResult, Usage
from app.agent.runtime.gemini import GeminiAgentRuntime
from tests.agent.recording._fakes import RecordingLlmCallRecorder
from tests.agent.runtime._helpers import FakeGeminiClient, make_agent
from tests.agent.runtime.test_streaming_contract import (
    FakeSdkStream,
    _stream_chunk,
    _usage,
)


def _runtime(
    stream: FakeSdkStream,
    recorder: RecordingLlmCallRecorder,
) -> GeminiAgentRuntime:
    return GeminiAgentRuntime(
        client=cast(
            AsyncClient,
            FakeGeminiClient([], streams=[stream]),
        ),
        llm_calls=recorder,
    )


async def test_normal_stream_eof_records_completed_succeeded() -> None:
    """正常終端の stream は completed / succeeded で閉じる。"""

    recorder = RecordingLlmCallRecorder()
    runtime = _runtime(
        FakeSdkStream(
            [
                _stream_chunk(
                    text="fragment",
                    finish_reason="STOP",
                    usage_metadata=_usage(),
                )
            ]
        ),
        recorder,
    )
    stream = runtime.stream_text(
        make_agent(response_schema=None),
        "typed input",
        attempt_number=1,
    )

    fragments = [fragment async for fragment in stream]

    assert fragments == ["fragment"]
    assert len(recorder.starts) == 1
    recorded = recorder.ends[0]
    assert recorded.result is LlmCallResult.SUCCEEDED
    assert recorded.stopped is False
    assert recorded.usage == Usage(
        input_tokens=11,
        output_tokens=7,
        cache_read_input_tokens=3,
        reasoning_output_tokens=2,
    )


async def test_consumer_aclose_records_stopped_and_keeps_usage() -> None:
    """途中 aclose は stopped にし、既に見た usage は残す。"""

    recorder = RecordingLlmCallRecorder()
    runtime = _runtime(
        FakeSdkStream([_stream_chunk(text="fragment", usage_metadata=_usage())]),
        recorder,
    )
    stream = runtime.stream_text(
        make_agent(response_schema=None),
        "typed input",
        attempt_number=1,
    )

    assert await stream.__anext__() == "fragment"
    await stream.aclose()
    await stream.aclose()

    assert len(recorder.starts) == 1
    assert len(recorder.ends) == 1
    recorded = recorder.ends[0]
    assert recorded.result is None
    assert recorded.stopped is True
    assert recorded.usage == Usage(
        input_tokens=11,
        output_tokens=7,
        cache_read_input_tokens=3,
        reasoning_output_tokens=2,
    )


async def test_cancellation_records_stopped_without_result() -> None:
    """stream 中の cancel は stopped、result なし。"""

    recorder = RecordingLlmCallRecorder()
    runtime = _runtime(
        FakeSdkStream([asyncio.CancelledError()]),
        recorder,
    )
    stream = runtime.stream_text(
        make_agent(response_schema=None),
        "typed input",
        attempt_number=1,
    )

    with pytest.raises(asyncio.CancelledError):
        await stream.__anext__()

    recorded = recorder.ends[0]
    assert recorded.result is None
    assert recorded.stopped is True
    assert recorded.usage is None
    assert len(recorder.ends) == 1
