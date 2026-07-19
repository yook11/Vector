"""ScriptedAgentRuntime のテスト double 契約。"""

from __future__ import annotations

import pytest

from tests.agent.runtime._fakes import AgentRuntimeCall, ScriptedAgentRuntime
from tests.agent.runtime._helpers import make_agent


async def test_scripted_runtime_returns_outcomes_in_fifo_order() -> None:
    """用意した結果を投入順に一件ずつ返す。"""
    first_outcome = object()
    second_outcome = object()
    runtime = ScriptedAgentRuntime(outcomes=[first_outcome, second_outcome])

    first_result = await runtime.invoke(make_agent(), object(), attempt_number=1)
    second_result = await runtime.invoke(make_agent(), object(), attempt_number=2)

    assert first_result is first_outcome
    assert second_result is second_outcome


async def test_scripted_runtime_records_exact_agent_input_and_attempt_number() -> None:
    """各呼出しの agent・入力・試行番号をそのまま観測可能にする。"""
    first_agent = make_agent(name="first_scripted_agent")
    second_agent = make_agent(name="second_scripted_agent")
    first_input = object()
    second_input = object()
    runtime = ScriptedAgentRuntime(outcomes=[object(), object()])

    await runtime.invoke(first_agent, first_input, attempt_number=3)
    await runtime.invoke(second_agent, second_input, attempt_number=7)

    assert runtime.calls == [
        AgentRuntimeCall(
            agent=first_agent,
            input=first_input,
            attempt_number=3,
        ),
        AgentRuntimeCall(
            agent=second_agent,
            input=second_input,
            attempt_number=7,
        ),
    ]


async def test_scripted_runtime_reraises_scripted_exception_by_identity() -> None:
    """script 済み例外の同一性を保って呼出し元へ伝播する。"""
    error = RuntimeError("scripted runtime failure")
    runtime = ScriptedAgentRuntime(outcomes=[error])

    with pytest.raises(RuntimeError) as raised:
        await runtime.invoke(make_agent(), object(), attempt_number=1)

    assert raised.value is error


async def test_scripted_runtime_rejects_calls_after_outcomes_are_exhausted() -> None:
    """結果が尽きた呼出しを原因が分かる assertion で拒否する。"""
    runtime = ScriptedAgentRuntime(outcomes=[])

    with pytest.raises(AssertionError) as raised:
        await runtime.invoke(make_agent(), object(), attempt_number=1)

    message = str(raised.value).lower()
    assert "scripted" in message
    assert "outcome" in message
    assert "exhaust" in message


def test_scripted_runtime_assert_all_outcomes_consumed_allows_empty_queue() -> None:
    """未使用結果がない script は明示的な消費確認を通過する。"""
    runtime = ScriptedAgentRuntime(outcomes=[])

    runtime.assert_all_outcomes_consumed()

    assert runtime.calls == []


def test_scripted_runtime_reports_remaining_outcome_count() -> None:
    """未使用結果の検出時に残数を assertion の文面へ含める。"""
    runtime = ScriptedAgentRuntime(outcomes=[object(), object()])

    with pytest.raises(AssertionError) as raised:
        runtime.assert_all_outcomes_consumed()

    assert "2" in str(raised.value)
