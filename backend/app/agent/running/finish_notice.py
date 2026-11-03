"""Runを終えたcommitの後に、利用枠の観測と終了通知を行う。"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import suppress
from uuid import UUID

from app.agent.live_updates.stream import (
    AgentRunLiveStreamPublisher,
    AgentRunLiveStreamTerminalEvent,
)
from app.agent.running.daily_quota import observability as daily_quota_observability
from app.agent.running.state import RunFinished
from app.log_policy.policies.research import RESEARCH_LOG_RULES
from app.log_policy.runtime import create_policy_json_logger

__all__ = ["notify_run_finished"]

logger = create_policy_json_logger(__name__, RESEARCH_LOG_RULES)


async def notify_run_finished(
    finished: RunFinished,
    *,
    run_id: UUID,
    publisher_for: Callable[[int], AgentRunLiveStreamPublisher],
) -> None:
    """queuedから終えたRunは利用枠の返却を観測し、runningから終えたRunはその世代へ終了を通知する。"""
    if finished.running_attempt_epoch is None:
        with suppress(Exception):
            daily_quota_observability.observe_release(
                run_id=run_id,
                outcome=finished.quota_release_outcome,
            )
        return
    event = AgentRunLiveStreamTerminalEvent.model_validate(
        {"status": finished.status.value, "errorCode": finished.error_code}
    )
    try:
        await publisher_for(finished.running_attempt_epoch).publish(event)
    except Exception:
        logger.warning(
            "agent_run_live_stream_terminal_publish_failed",
            run_id=str(run_id),
            terminal_status=event.status,
        )
