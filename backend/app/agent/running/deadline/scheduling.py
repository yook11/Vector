"""受付済みrunの期限確認を、回答実行を待たせず予約する。"""

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import UUID

import structlog
from fastapi import Request
from taskiq import ScheduleSource

logger = structlog.get_logger(__name__)
DEADLINE_RESERVATION_TIMEOUT_SECONDS = 2


def deadline_check_time(deadline_at: datetime) -> datetime:
    if deadline_at.tzinfo is None:
        raise ValueError("deadline requires a timezone")
    at = deadline_at.astimezone(UTC)
    return (
        at if at.microsecond == 0 else at.replace(microsecond=0) + timedelta(seconds=1)
    )


class AgentDeadlineScheduler:
    def __init__(self, source: ScheduleSource) -> None:
        self._source = source

    async def reserve(self, run_id: UUID, deadline_at: datetime) -> None:
        from app.queue.messages.agent_run import AgentRunTrigger
        from app.queue.tasks.agent_run import check_agent_run_deadline

        try:
            async with asyncio.timeout(DEADLINE_RESERVATION_TIMEOUT_SECONDS):
                await check_agent_run_deadline.schedule_by_time(
                    self._source,
                    deadline_check_time(deadline_at),
                    AgentRunTrigger(run_id=run_id).model_dump(mode="json"),
                )
        except Exception as exc:
            logger.warning(
                "agent_run_deadline_reservation_failed",
                run_id=str(run_id),
                error_type=type(exc).__name__,
            )


def get_agent_deadline_scheduler(request: Request) -> AgentDeadlineScheduler:
    return request.app.state.agent_deadline_scheduler
