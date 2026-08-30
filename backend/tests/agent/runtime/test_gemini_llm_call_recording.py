"""GeminiAgentRuntime の LLM call 記録契約。"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from types import SimpleNamespace

import pytest

from app.agent.recording.types import Usage
from app.agent.runtime.contract import AgentResponseDefect, AgentResponseInvalidError
from app.agent.runtime.gemini import GeminiAgentRuntime
from app.agent.runtime.llm_failure import UNCLASSIFIED_FAILURE_CODE, LlmAttemptFailed
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


async def test_successful_call_records_usage_without_failure() -> None:
    """成功 attempt は結果を返し、失敗型を報告しない。"""

    recorder = RecordingLlmCallRecorder()
    runtime = _runtime(
        [success_response(usage_metadata=_usage())],
        recorder,
    )
    agent = make_agent()

    await runtime.call(agent, "typed input", attempt_number=1)

    assert len(recorder.records) == 1
    recorded = recorder.records[0]
    assert recorded.agent_name == agent.name
    assert recorded.provider == "gemini"
    assert recorded.model == agent.model.name
    assert recorded.attempt_number == 1
    assert recorded.mode == "call"
    assert recorded.prompt_version == agent.prompt.version
    assert recorded.operation_name == "generate_content"
    assert recorded.gen_ai_provider == "gcp.gemini"
    assert recorded.failure is None
    assert recorded.error is None
    assert recorded.usage == Usage(
        input_tokens=11,
        output_tokens=7,
        cache_read_input_tokens=3,
        reasoning_output_tokens=2,
    )


async def test_blocked_call_records_failed_with_code() -> None:
    """出力 block は分類済み失敗と CODE で閉じる。"""

    recorder = RecordingLlmCallRecorder()
    runtime = _runtime(
        [blocked_response("SAFETY", usage_metadata=_usage())],
        recorder,
    )

    with pytest.raises(AIProviderOutputBlockedError):
        await runtime.call(make_agent(), "typed input", attempt_number=1)

    recorded = recorder.records[0]
    assert recorded.failure == LlmAttemptFailed(
        failure_code=AIProviderOutputBlockedError.CODE
    )
    assert isinstance(recorded.error, AIProviderOutputBlockedError)
    assert recorded.usage == Usage(
        input_tokens=11,
        output_tokens=7,
        cache_read_input_tokens=3,
        reasoning_output_tokens=2,
    )


async def test_invalid_response_records_failed_with_defect() -> None:
    """不正応答は分類済み失敗と defect で閉じる。"""

    recorder = RecordingLlmCallRecorder()
    runtime = _runtime([FakeResponse(text="MODEL_OUTPUT_NOT_JSON")], recorder)

    with pytest.raises(AgentResponseInvalidError):
        await runtime.call(make_agent(), "typed input", attempt_number=1)

    recorded = recorder.records[0]
    assert recorded.failure == LlmAttemptFailed(
        failure_code=AgentResponseDefect.RESPONSE_NOT_JSON
    )


async def test_translated_provider_error_records_failed_with_code() -> None:
    """翻訳済み provider 障害は分類済み失敗と CODE で閉じる。"""

    recorder = RecordingLlmCallRecorder()
    runtime = _runtime([TimeoutError("timeout")], recorder)

    with pytest.raises(AIProviderNetworkError):
        await runtime.call(make_agent(), "typed input", attempt_number=1)

    recorded = recorder.records[0]
    assert recorded.failure == LlmAttemptFailed(
        failure_code=AIProviderNetworkError.CODE
    )
    assert recorded.usage is None


async def test_unclassified_exception_records_unclassified_failure() -> None:
    """未分類例外は unclassified として報告し、同じ例外を raise する。"""

    error = RuntimeError("UNCLASSIFIED_EXCEPTION_SENTINEL")
    recorder = RecordingLlmCallRecorder()
    runtime = _runtime([error], recorder)

    with pytest.raises(RuntimeError) as exc_info:
        await runtime.call(make_agent(), "typed input", attempt_number=1)

    assert exc_info.value is error
    recorded = recorder.records[0]
    assert recorded.failure == LlmAttemptFailed(failure_code=UNCLASSIFIED_FAILURE_CODE)
    assert recorded.error is error


async def test_cancelled_call_records_stopped() -> None:
    """呼び出し中の cancel は停止として閉じる。"""

    recorder = RecordingLlmCallRecorder()
    runtime = _runtime([asyncio.CancelledError()], recorder)

    with pytest.raises(asyncio.CancelledError):
        await runtime.call(make_agent(), "typed input", attempt_number=1)

    recorded = recorder.records[0]
    assert recorded.failure is None
    assert isinstance(recorded.error, asyncio.CancelledError)
    assert len(recorder.records) == 1


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
    assert recorder.records == []
