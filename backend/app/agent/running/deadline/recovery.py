"""workerによる期限切れ回収の実行と、回収後のログ。"""

from __future__ import annotations

from uuid import UUID

import redis.asyncio as aioredis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent.live_updates.stream import AgentRunLiveStreamPublisher
from app.agent.running.deadline.deadline_exceeded import DeadlineRunSweepResult
from app.agent.running.state_service import AgentRunStateService
from app.log_policy.policies.research import RESEARCH_LOG_RULES
from app.log_policy.runtime import create_policy_json_logger

logger = create_policy_json_logger(__name__, RESEARCH_LOG_RULES)


async def sweep_and_report_deadline_exceeded_runs(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    live_redis: aioredis.Redis,
) -> None:
    result = await _state_service(
        session_factory, live_redis
    ).sweep_deadline_exceeded_runs()
    _report_deadline_recovery(result)


async def check_and_report_run_deadline(
    run_id: UUID,
    *,
    session_factory: async_sessionmaker[AsyncSession],
    live_redis: aioredis.Redis,
) -> None:
    result = await _state_service(session_factory, live_redis).check_run_deadline(
        run_id
    )
    _report_deadline_recovery(result)


def _state_service(
    session_factory: async_sessionmaker[AsyncSession],
    live_redis: aioredis.Redis,
) -> AgentRunStateService:
    return AgentRunStateService(
        session_factory=session_factory,
        publisher_for=lambda attempt: AgentRunLiveStreamPublisher(live_redis, attempt),
    )


def _report_deadline_recovery(result: DeadlineRunSweepResult) -> None:
    logger.info("agent_runs_deadline_swept", count=result.total_count)
    if result.queued_terminal_count > 0:
        logger.info(
            "agent_runs_queued_deadline_swept",
            run_count=result.queued_terminal_count,
            quota_released_count=result.queued_quota_released_count,
            quota_not_eligible_count=result.queued_quota_not_eligible_count,
            quota_inconsistent_count=result.queued_quota_inconsistent_count,
        )
    if result.running_terminal_runs:
        logger.info(
            "running_deadline_exceeded_swept",
            count=len(result.running_terminal_runs),
        )
