"""所有スレッドの詳細取得と、そのスレッドに限定した期限回収。"""

from __future__ import annotations

from contextlib import suppress
from uuid import UUID

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.daily_quota import observability as daily_quota_observability
from app.agent.live_updates.stream import AgentRunLiveStreamTerminalEvent
from app.agent.live_updates.transport import AgentLiveTransport
from app.agent.run_deadline.contracts import DeadlineRunSweepResult
from app.agent.run_deadline.persistence import (
    sweep_deadline_exceeded_runs_for_thread,
)
from app.agent.threads.repository import AgentThreadRepository
from app.models.agent_thread import AgentThread
from app.schemas.research import ResearchThreadDetail

logger = structlog.get_logger(__name__)


async def read_owned_thread_detail(
    session: AsyncSession,
    *,
    thread_id: UUID,
    user_id: UUID,
    live: AgentLiveTransport,
) -> ResearchThreadDetail | None:
    repo = AgentThreadRepository(session)
    async with session.begin():
        owned = await session.scalar(
            select(AgentThread.id).where(
                AgentThread.id == thread_id,
                AgentThread.user_id == user_id,
            )
        )
        if owned is None:
            return None
        result = await sweep_deadline_exceeded_runs_for_thread(
            session, thread_id=thread_id
        )
    session.expire_all()
    _observe_recovery(result)
    await _notify_recovered_running(live, result)
    return await repo.read_thread_detail_for_user(
        thread_id=thread_id,
        user_id=user_id,
    )


def _observe_recovery(result: DeadlineRunSweepResult) -> None:
    for quota_result, count in (
        ("released", result.queued_quota_released_count),
        ("not_eligible", result.queued_quota_not_eligible_count),
        ("inconsistent", result.queued_quota_inconsistent_count),
    ):
        if count > 0:
            with suppress(Exception):
                daily_quota_observability.record_daily_quota_release(
                    result=quota_result,
                    count=count,
                )
    with suppress(Exception):
        daily_quota_observability.observe_stale_reservations(
            queued_count=result.queued_quota_inconsistent_count,
            running_count=result.running_quota_reservation_count,
        )


async def _notify_recovered_running(
    live: AgentLiveTransport,
    result: DeadlineRunSweepResult,
) -> None:
    for running_run in result.running_terminal_runs:
        try:
            await live.publisher(running_run.run_id, running_run.attempt_epoch).publish(
                AgentRunLiveStreamTerminalEvent(status="deadline_exceeded")
            )
        except Exception:
            logger.warning(
                "agent_run_live_stream_terminal_publish_failed",
                run_id=str(running_run.run_id),
                terminal_status="deadline_exceeded",
            )
