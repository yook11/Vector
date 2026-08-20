"""記録語彙の閉じた値とハンドル形。"""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from app.agent.recording.types import (
    LlmCall,
    LlmCallResult,
    PhaseCall,
    PhaseStatus,
    ToolCall,
    Usage,
)


def test_phase_status_has_three_closed_values() -> None:
    """作業単位の終わり方は completed / failed / stopped だけ。"""

    assert [status.value for status in PhaseStatus] == [
        "completed",
        "failed",
        "stopped",
    ]


def test_llm_call_result_matches_provider_span_result_strings() -> None:
    """LLM 結論は span の result と同じ文字列。"""

    assert [result.value for result in LlmCallResult] == [
        "succeeded",
        "blocked",
        "invalid_response",
        "provider_error",
    ]


def test_start_handles_are_frozen() -> None:
    """start ハンドルは後から書き換えられない。"""

    llm_call = LlmCall(
        agent_name="question_planner",
        provider="gemini",
        model="gemini-test-model",
        attempt_number=1,
        started_at=1.0,
    )
    tool_call = ToolCall(tool_name="tavily_search", started_at=1.0)
    phase_call = PhaseCall(started_at=1.0)
    usage = Usage(input_tokens=3)

    with pytest.raises(FrozenInstanceError):
        llm_call.agent_name = "other"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        tool_call.tool_name = "other"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        phase_call.started_at = 2.0  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        usage.input_tokens = 0  # type: ignore[misc]
