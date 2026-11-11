"""Runを終了させた操作のcommit後に、実行中だった試行の画面へ終了を通知する。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from app.agent.live_updates.stream import (
    AgentRunLiveStreamPublisher,
    AgentRunLiveStreamTerminalEvent,
)
from app.agent.running.types import AgentRunErrorCode, AgentRunStatus, RunAttempt
from app.log_policy.policies.research import RESEARCH_LOG_RULES
from app.log_policy.runtime import create_policy_json_logger

__all__ = ["ClosedRunningRun", "LivePublisherFor", "notify_closed_run"]

logger = create_policy_json_logger(__name__, RESEARCH_LOG_RULES)

type LivePublisherFor = Callable[[RunAttempt], AgentRunLiveStreamPublisher]


@dataclass(frozen=True, slots=True)
class ClosedRunningRun:
    """実行中の試行から閉じたRun。その世代の画面へ終了を知らせる。"""

    attempt: RunAttempt
    status: AgentRunStatus
    error_code: AgentRunErrorCode | None

    def __post_init__(self) -> None:
        if self.status in (AgentRunStatus.QUEUED, AgentRunStatus.RUNNING):
            raise ValueError("closed running run requires a terminal status")
        if (self.status is AgentRunStatus.FAILED) != (self.error_code is not None):
            raise ValueError("only a failed run carries an error code")


async def notify_closed_run(
    closed: ClosedRunningRun, *, publisher_for: LivePublisherFor
) -> None:
    """commitの後に呼び、その世代の画面へ終了を通知する。"""
    event = AgentRunLiveStreamTerminalEvent.model_validate(
        {"status": closed.status.value, "errorCode": closed.error_code}
    )
    try:
        await publisher_for(closed.attempt).publish(event)
    except Exception:
        logger.warning(
            "agent_run_live_stream_terminal_publish_failed",
            run_id=str(closed.attempt.run_id),
            terminal_status=event.status,
        )
