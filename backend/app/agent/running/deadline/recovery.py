"""workerによる期限切れ回収の実行と、回収後のログ。"""

from __future__ import annotations

from uuid import UUID

import redis.asyncio as aioredis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent.live_updates.stream import AgentRunLiveStreamPublisher
from app.agent.running.state_service import AgentRunStateService
from app.agent.running.types import RunAttempt
from app.log_policy.policies.research import RESEARCH_LOG_RULES
from app.log_policy.runtime import create_policy_json_logger

logger = create_policy_json_logger(__name__, RESEARCH_LOG_RULES)


async def sweep_and_report_deadline_exceeded_runs(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    live_redis: aioredis.Redis,
) -> None:
    running_attempts = await _state_service(
        session_factory, live_redis
    ).end_runs_past_deadline()
    _report_deadline_recovery(running_attempts)


async def check_and_report_run_deadline(
    run_id: UUID,
    *,
    session_factory: async_sessionmaker[AsyncSession],
    live_redis: aioredis.Redis,
) -> None:
    running_attempts = await _state_service(
        session_factory, live_redis
    ).end_runs_past_deadline(run_id=run_id)
    _report_deadline_recovery(running_attempts)


def _state_service(
    session_factory: async_sessionmaker[AsyncSession],
    live_redis: aioredis.Redis,
) -> AgentRunStateService:
    return AgentRunStateService(
        session_factory=session_factory,
        publisher_for=lambda attempt: AgentRunLiveStreamPublisher(live_redis, attempt),
    )


def _report_deadline_recovery(running_attempts: tuple[RunAttempt, ...]) -> None:
    if running_attempts:
        logger.info("running_deadline_exceeded_swept", count=len(running_attempts))
