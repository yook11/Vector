"""Gemini stream の LLM call 記録契約。"""

from __future__ import annotations

import asyncio
from typing import cast

import pytest
from google.genai.client import AsyncClient

from app.agent.recording.types import Usage
from app.agent.runtime.gemini import GeminiAgentRuntime
from app.agent.runtime.llm_failure import UNCLASSIFIED_FAILURE_CODE, LlmAttemptFailed
from app.analysis.ai_provider_errors import AIProviderOutputBlockedError
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


async def test_normal_stream_eof_records_usage_without_failure() -> None:
    """正常終端の stream は失敗型なしで閉じる。"""

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
    assert len(recorder.records) == 1
    recorded = recorder.records[0]
    assert recorded.mode == "stream"
    assert recorded.parent_context is not None
    assert recorded.failure is None
    assert recorded.error is None
    assert recorded.usage == Usage(
        input_tokens=11,
        output_tokens=7,
        cache_read_input_tokens=3,
        reasoning_output_tokens=2,
    )


async def test_blocked_stream_records_failed_with_code() -> None:
    """出力 block の stream は分類済み失敗と CODE で閉じる。"""

    recorder = RecordingLlmCallRecorder()
    runtime = _runtime(
        FakeSdkStream([_stream_chunk(finish_reason="SAFETY", usage_metadata=_usage())]),
        recorder,
    )
    stream = runtime.stream_text(
        make_agent(response_schema=None),
        "typed input",
        attempt_number=1,
    )

    with pytest.raises(AIProviderOutputBlockedError):
        _ = [fragment async for fragment in stream]

    recorded = recorder.records[0]
    assert recorded.failure == LlmAttemptFailed(
        failure_code=AIProviderOutputBlockedError.CODE
    )
    assert recorded.usage == Usage(
        input_tokens=11,
        output_tokens=7,
        cache_read_input_tokens=3,
        reasoning_output_tokens=2,
    )


async def test_unclassified_stream_records_unclassified_failure() -> None:
    """未分類例外の stream は unclassified として報告し、同じ例外を raise する。"""

    error = RuntimeError("UNCLASSIFIED_STREAM")
    recorder = RecordingLlmCallRecorder()
    runtime = _runtime(FakeSdkStream([error]), recorder)
    stream = runtime.stream_text(
        make_agent(response_schema=None),
        "typed input",
        attempt_number=1,
    )

    with pytest.raises(RuntimeError) as exc_info:
        await stream.__anext__()

    assert exc_info.value is error
    recorded = recorder.records[0]
    assert recorded.failure == LlmAttemptFailed(failure_code=UNCLASSIFIED_FAILURE_CODE)
    assert recorded.error is error


async def test_consumer_aclose_records_stopped_and_keeps_usage() -> None:
    """途中 aclose は停止にし、既に見た usage は残す。"""

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

    assert len(recorder.records) == 1
    recorded = recorder.records[0]
    assert recorded.failure is None
    assert isinstance(recorded.error, GeneratorExit)
    assert recorded.usage == Usage(
        input_tokens=11,
        output_tokens=7,
        cache_read_input_tokens=3,
        reasoning_output_tokens=2,
    )


async def test_cancellation_records_stopped_without_failure() -> None:
    """stream 中の cancel は停止、失敗型なし。"""

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

    recorded = recorder.records[0]
    assert recorded.failure is None
    assert isinstance(recorded.error, asyncio.CancelledError)
    assert recorded.usage is None
    assert len(recorder.records) == 1
