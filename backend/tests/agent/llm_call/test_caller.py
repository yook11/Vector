"""LLM呼び出しの差し替え口の契約と call_agent。偽の口を直接渡す(外部依存なし)。"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from app.agent.llm_call.caller import call_agent
from app.agent.output import AgentOutputDefect, AgentOutputInvalidError
from tests.agent.llm_call._fakes import RecordedLlmCall, ScriptedLlmCaller
from tests.agent.llm_call._helpers import CallerOutput, make_agent


def test_contract_import_does_not_eagerly_import_gemini_caller() -> None:
    """中立 contract の import が Gemini 依存を導入しないことを守る。"""
    backend_dir = Path(__file__).resolve().parents[3]
    script = """
import sys
import app.agent.llm_call.caller

raise SystemExit(int("app.agent.llm_call.gemini" in sys.modules))
"""

    completed = subprocess.run(  # noqa: S603
        [sys.executable, "-c", script],
        cwd=backend_dir,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr or completed.stdout


async def test_call_agent_returns_the_output_validated_by_the_called_agent() -> None:
    """口へ渡したAgentの出力の型で、口が返したJSON objectを検証して返す。"""
    agent = make_agent()
    caller = ScriptedLlmCaller([{"result": "accepted", "tags": ["caller"]}])

    output = await call_agent(caller, agent, "typed input", attempt_number=2)

    assert output == CallerOutput(result="accepted", tags=["caller"])
    assert caller.calls == [
        RecordedLlmCall(agent=agent, input="typed input", attempt_number=2)
    ]


async def test_call_agent_rejects_a_payload_that_does_not_match_the_output_type() -> (
    None
):
    """出力の型に合わないJSON objectは、出力の契約違反として送出する。"""
    caller = ScriptedLlmCaller([{"result": "accepted"}])

    with pytest.raises(AgentOutputInvalidError) as exc_info:
        await call_agent(caller, make_agent(), "typed input", attempt_number=1)

    assert exc_info.value.defect is AgentOutputDefect.OUTPUT_SCHEMA_MISMATCH
