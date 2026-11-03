"""AgentRunStateRepository.start_attempt の戻り値 assertion。"""

from __future__ import annotations

from app.agent.running.state import AttemptStarted


def started_attempt_epoch(result: object) -> int:
    """開始成功の attempt epoch を取り出す。"""
    assert isinstance(result, AttemptStarted)
    return result.attempt_epoch
