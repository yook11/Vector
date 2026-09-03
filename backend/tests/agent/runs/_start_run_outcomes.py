"""AgentRunRepository start_run の戻り値 assertion。"""

from __future__ import annotations

from app.agent.runs.contracts import StartRunFailure, StartRunFailureReason


def started_attempt_epoch(result: object) -> int:
    """開始成功の attempt epoch を取り出す。"""
    assert isinstance(result, int)
    assert not isinstance(result, bool)
    assert result >= 1
    return result


def assert_start_failure(result: object, reason: StartRunFailureReason) -> None:
    """開始しなかった理由を確認する。"""
    assert isinstance(result, StartRunFailure)
    assert result.reason is reason
