"""受付済みrunの期限確認を、回答実行を待たせず予約する。"""

import asyncio
from contextlib import suppress
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
        self._pending: set[asyncio.Task[None]] = set()
        self._closing = False

    def reserve_in_background(self, run_id: UUID, deadline_at: datetime) -> None:
        if self._closing:
            return
        reservation = self.reserve(run_id, deadline_at)
        try:
            task = asyncio.create_task(reservation)
        except Exception as exc:
            reservation.close()
            self._log_failure(run_id, exc)
            return
        self._pending.add(task)

        def finished(task: asyncio.Task[None]) -> None:
            self._pending.discard(task)
            if not task.cancelled():
                error = task.exception()
                if error is not None:
                    self._log_failure(run_id, error)

        task.add_done_callback(finished)

    async def cancel_pending_reservations(self) -> None:
        self._closing = True
        pending = tuple(self._pending)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        self._pending.clear()

    @staticmethod
    def _log_failure(run_id: UUID, error: BaseException) -> None:
        with suppress(Exception):
            logger.warning(
                "agent_run_deadline_reservation_failed",
                run_id=str(run_id),
                error_type=type(error).__name__,
            )

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
            self._log_failure(run_id, exc)


def get_agent_deadline_scheduler(request: Request) -> AgentDeadlineScheduler:
    return request.app.state.agent_deadline_scheduler
