"""GeminiAgentRuntime の LLM call 記録契約。"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from types import SimpleNamespace

import pytest

from app.agent.recording.types import LlmCallResult, PhaseStatus, Usage
from app.agent.runtime.contract import AgentResponseInvalidError
from app.agent.runtime.gemini import GeminiAgentRuntime
from app.analysis.ai_provider_errors import (
    AIProviderNetworkError,
    AIProviderOutputBlockedError,
)
from tests.agent.recording._fakes import RecordingLlmCallRecorder
from tests.agent.runtime._helpers import (
    FakeGeminiClient,
    FakeResponse,
    blocked_response,
    make_agent,
    success_response,
)


def _usage() -> SimpleNamespace:
    return SimpleNamespace(
        prompt_token_count=11,
        candidates_token_count=7,
        cached_content_token_count=3,
        thoughts_token_count=2,
    )


def _runtime(
    responses: list[object],
    recorder: RecordingLlmCallRecorder,
) -> GeminiAgentRuntime:
    return GeminiAgentRuntime(
        client=FakeGeminiClient(responses),
        llm_calls=recorder,
    )


async def test_successful_call_records_completed_succeeded_usage() -> None:
    """成功 attempt は completed / succeeded と token を残す。"""

    recorder = RecordingLlmCallRecorder()
    runtime = _runtime(
        [success_response(usage_metadata=_usage())],
        recorder,
    )
    agent = make_agent()

    await runtime.call(agent, "typed input", attempt_number=1)

    assert len(recorder.starts) == 1
    assert len(recorder.ends) == 1
    recorded = recorder.ends[0]
    assert recorded.call is recorder.starts[0]
    assert recorded.call.agent_name == agent.name
    assert recorded.call.provider == "gemini"
    assert recorded.call.model == agent.model.name
    assert recorded.call.attempt_number == 1
    assert recorded.status is PhaseStatus.COMPLETED
    assert recorded.result is LlmCallResult.SUCCEEDED
    assert recorded.usage == Usage(
        input_tokens=11,
        output_tokens=7,
        cache_read_input_tokens=3,
        reasoning_output_tokens=2,
    )


async def test_blocked_call_records_failed_blocked() -> None:
    """出力 block は failed / blocked で閉じる。"""

    recorder = RecordingLlmCallRecorder()
    runtime = _runtime(
        [blocked_response("SAFETY", usage_metadata=_usage())],
        recorder,
    )

    with pytest.raises(AIProviderOutputBlockedError):
        await runtime.call(make_agent(), "typed input", attempt_number=1)

    recorded = recorder.ends[0]
    assert recorded.status is PhaseStatus.FAILED
    assert recorded.result is LlmCallResult.BLOCKED
    assert recorded.usage == Usage(
        input_tokens=11,
        output_tokens=7,
        cache_read_input_tokens=3,
        reasoning_output_tokens=2,
    )


async def test_invalid_response_records_failed_invalid_response() -> None:
    """不正応答は failed / invalid_response で閉じる。"""

    recorder = RecordingLlmCallRecorder()
    runtime = _runtime([FakeResponse(text="MODEL_OUTPUT_NOT_JSON")], recorder)

    with pytest.raises(AgentResponseInvalidError):
        await runtime.call(make_agent(), "typed input", attempt_number=1)

    recorded = recorder.ends[0]
    assert recorded.status is PhaseStatus.FAILED
    assert recorded.result is LlmCallResult.INVALID_RESPONSE


async def test_translated_provider_error_records_failed_provider_error() -> None:
    """翻訳済み provider 障害は failed / provider_error で閉じる。"""

    recorder = RecordingLlmCallRecorder()
    runtime = _runtime([TimeoutError("timeout")], recorder)

    with pytest.raises(AIProviderNetworkError):
        await runtime.call(make_agent(), "typed input", attempt_number=1)

    recorded = recorder.ends[0]
    assert recorded.status is PhaseStatus.FAILED
    assert recorded.result is LlmCallResult.PROVIDER_ERROR
    assert recorded.usage is None


async def test_unclassified_exception_records_failed_without_result() -> None:
    """未分類例外は failed、result なしで閉じる。"""

    error = RuntimeError("UNCLASSIFIED_EXCEPTION_SENTINEL")
    recorder = RecordingLlmCallRecorder()
    runtime = _runtime([error], recorder)

    with pytest.raises(RuntimeError) as exc_info:
        await runtime.call(make_agent(), "typed input", attempt_number=1)

    assert exc_info.value is error
    recorded = recorder.ends[0]
    assert recorded.status is PhaseStatus.FAILED
    assert recorded.result is None


async def test_cancelled_call_records_stopped() -> None:
    """呼び出し中の cancel は stopped で閉じる。"""

    recorder = RecordingLlmCallRecorder()
    runtime = _runtime([asyncio.CancelledError()], recorder)

    with pytest.raises(asyncio.CancelledError):
        await runtime.call(make_agent(), "typed input", attempt_number=1)

    recorded = recorder.ends[0]
    assert recorded.status is PhaseStatus.STOPPED
    assert recorded.result is None
    assert len(recorder.starts) == 1
    assert len(recorder.ends) == 1


async def test_renderer_failure_does_not_start_recording() -> None:
    """描画失敗では provider attempt を記録しない。"""

    error = RuntimeError("RENDERER_FAILURE")
    recorder = RecordingLlmCallRecorder()
    agent = make_agent()
    agent = replace(
        agent,
        prompt=type(agent.prompt)(
            version=agent.prompt.version,
            instructions=agent.prompt.instructions,
            input_renderer=lambda _input: (_ for _ in ()).throw(error),
        ),
    )
    runtime = _runtime([success_response()], recorder)

    with pytest.raises(RuntimeError) as exc_info:
        await runtime.call(agent, "typed input", attempt_number=1)

    assert exc_info.value is error
    assert recorder.starts == []
    assert recorder.ends == []
