"""DeepSeekAgentRuntime の LLM call 記録契約。"""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest

from app.agent.recording.types import LlmCallResult, PhaseStatus, Usage
from app.agent.runtime.contract import AgentResponseInvalidError
from app.agent.runtime.deepseek import DeepSeekAgentRuntime
from app.analysis.ai_provider_errors import AIProviderNetworkError
from tests.agent.recording._fakes import RecordedLlmCallEnd, RecordingLlmCallRecorder
from tests.agent.runtime._deepseek_helpers import (
    FakeDeepSeekClient,
    function_response,
    make_agent,
    make_binding,
    success_response,
)


def _usage() -> SimpleNamespace:
    return SimpleNamespace(
        prompt_tokens=4,
        completion_tokens=6,
        prompt_tokens_details=SimpleNamespace(cached_tokens=1),
        completion_tokens_details=SimpleNamespace(reasoning_tokens=2),
    )


def _runtime(
    responses: list[object],
    recorder: RecordingLlmCallRecorder,
) -> DeepSeekAgentRuntime:
    return DeepSeekAgentRuntime(
        client=FakeDeepSeekClient(responses),
        binding=make_binding(),
        llm_calls=recorder,
    )


async def test_successful_call_records_completed_succeeded_usage() -> None:
    """成功 attempt は completed / succeeded と token を残す。"""

    recorder = RecordingLlmCallRecorder()
    runtime = _runtime([success_response(usage=_usage())], recorder)
    agent = make_agent()

    await runtime.call(agent, object(), attempt_number=1)

    assert len(recorder.starts) == 1
    started = recorder.starts[0]
    assert started.agent_name == agent.name
    assert started.provider == "deepseek"
    assert started.model == agent.model.name
    assert recorder.ends == [
        RecordedLlmCallEnd(
            call=started,
            status=PhaseStatus.COMPLETED,
            result=LlmCallResult.SUCCEEDED,
            usage=Usage(
                input_tokens=4,
                output_tokens=6,
                cache_read_input_tokens=1,
                reasoning_output_tokens=2,
            ),
        )
    ]


async def test_invalid_response_records_failed_invalid_response() -> None:
    """不正な function 出力は failed / invalid_response で閉じる。"""

    recorder = RecordingLlmCallRecorder()
    runtime = _runtime([function_response(no_tool_calls=True)], recorder)

    with pytest.raises(AgentResponseInvalidError):
        await runtime.call(make_agent(), object(), attempt_number=1)

    recorded = recorder.ends[0]
    assert recorded.status is PhaseStatus.FAILED
    assert recorded.result is LlmCallResult.INVALID_RESPONSE


async def test_translated_provider_error_records_failed_provider_error() -> None:
    """翻訳済み障害は failed / provider_error で閉じる。"""

    recorder = RecordingLlmCallRecorder()
    runtime = _runtime([TimeoutError("timeout")], recorder)

    with pytest.raises(AIProviderNetworkError):
        await runtime.call(make_agent(), object(), attempt_number=1)

    recorded = recorder.ends[0]
    assert recorded.status is PhaseStatus.FAILED
    assert recorded.result is LlmCallResult.PROVIDER_ERROR


async def test_unclassified_exception_records_failed_without_result() -> None:
    """未分類例外は failed、result なしで閉じる。"""

    error = RuntimeError("UNCLASSIFIED_DEEPSEEK")
    recorder = RecordingLlmCallRecorder()
    runtime = _runtime([error], recorder)

    with pytest.raises(RuntimeError) as exc_info:
        await runtime.call(make_agent(), object(), attempt_number=1)

    assert exc_info.value is error
    recorded = recorder.ends[0]
    assert recorded.status is PhaseStatus.FAILED
    assert recorded.result is None
    assert len(recorder.ends) == 1


async def test_request_build_failure_does_not_start_recording() -> None:
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
        await runtime.call(agent, object(), attempt_number=1)

    assert exc_info.value is error
    assert recorder.starts == []
    assert recorder.ends == []
