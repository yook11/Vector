"""Provider-neutral AgentRuntime contract tests."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from app.agent.runtime.contract import AgentResponseDefect, AgentResponseInvalidError


def test_agent_response_defect_has_only_three_provider_neutral_values() -> None:
    """応答不備の公開語彙を三つの中立値に限定する。"""
    assert [defect.value for defect in AgentResponseDefect] == [
        "response_not_json",
        "response_not_object",
        "output_schema_mismatch",
    ]


def test_agent_response_invalid_error_string_uses_defect_and_repair_hint() -> None:
    """安全な修正情報を defect と repair hint で利用可能にする。"""
    repair_hint = "field=score type=greater_than_equal ge=1"
    error = AgentResponseInvalidError(
        AgentResponseDefect.OUTPUT_SCHEMA_MISMATCH,
        repair_hint=repair_hint,
    )

    assert error.defect is AgentResponseDefect.OUTPUT_SCHEMA_MISMATCH
    assert error.repair_hint == repair_hint
    assert AgentResponseDefect.OUTPUT_SCHEMA_MISMATCH.value in str(error)
    assert repair_hint in str(error)


def test_contract_import_does_not_eagerly_import_gemini_runtime() -> None:
    """中立 contract の import が Gemini 依存を導入しないことを守る。"""
    backend_dir = Path(__file__).resolve().parents[3]
    script = """
import sys
import app.agent.runtime.contract

raise SystemExit(int("app.agent.runtime.gemini" in sys.modules))
"""

    completed = subprocess.run(  # noqa: S603
        [sys.executable, "-c", script],
        cwd=backend_dir,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr or completed.stdout
