"""Provider-neutral AgentRuntime contract tests."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


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
