"""Runを閉じた操作のcommit後に、利用枠の観測と終了通知を行う。"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from uuid import UUID

from app.agent.live_updates.stream import (
    AgentRunLiveStreamPublisher,
    AgentRunLiveStreamTerminalEvent,
)
from app.agent.running.daily_quota import observability as daily_quota_observability
from app.agent.running.daily_quota.release import DailyQuotaReleaseOutcome
from app.agent.running.types import AgentRunErrorCode, AgentRunStatus, RunAttempt
from app.log_policy.policies.research import RESEARCH_LOG_RULES
from app.log_policy.runtime import create_policy_json_logger

__all__ = [
    "ClosedQueuedRun",
    "ClosedRun",
    "ClosedRunningRun",
    "LivePublisherFor",
    "notify_closed_run",
]

logger = create_policy_json_logger(__name__, RESEARCH_LOG_RULES)

type LivePublisherFor = Callable[[RunAttempt], AgentRunLiveStreamPublisher]


@dataclass(frozen=True, slots=True)
class ClosedQueuedRun:
    """待機中のまま閉じたRun。予約していた利用枠を返している。"""

    run_id: UUID
    quota_release_outcome: DailyQuotaReleaseOutcome


@dataclass(frozen=True, slots=True)
class ClosedRunningRun:
    """実行中の試行から閉じたRun。その世代の画面へ終了を知らせる。"""

    attempt: RunAttempt
    status: AgentRunStatus
    error_code: AgentRunErrorCode | None

    def __post_init__(self) -> None:
        # commit前に作るので、通知できない組み合わせはここで止めてロールバックさせる。
        if self.status in (AgentRunStatus.QUEUED, AgentRunStatus.RUNNING):
            raise ValueError("closed running run requires a terminal status")
        if (self.status is AgentRunStatus.FAILED) != (self.error_code is not None):
            raise ValueError("only a failed run carries an error code")


type ClosedRun = ClosedQueuedRun | ClosedRunningRun


async def notify_closed_run(
    closed: ClosedRun, *, publisher_for: LivePublisherFor
) -> None:
    """commitの後に呼ぶ。待機中なら利用枠の返却を観測し、実行中ならその世代へ終了を通知する。"""
    match closed:
        case ClosedQueuedRun(run_id=run_id, quota_release_outcome=outcome):
            with suppress(Exception):
                daily_quota_observability.observe_release(
                    run_id=run_id, outcome=outcome
                )
        case ClosedRunningRun(attempt=attempt, status=status, error_code=error_code):
            event = AgentRunLiveStreamTerminalEvent.model_validate(
                {"status": status.value, "errorCode": error_code}
            )
            try:
                await publisher_for(attempt).publish(event)
            except Exception:
                logger.warning(
                    "agent_run_live_stream_terminal_publish_failed",
                    run_id=str(attempt.run_id),
                    terminal_status=event.status,
                )
