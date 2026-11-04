"""workerによる期限切れ回収の実行と、回収後のログ・利用枠の観測・終了通知。"""

from __future__ import annotations

from contextlib import suppress
from uuid import UUID

import redis.asyncio as aioredis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent.live_updates.stream import (
    AgentRunLiveStreamPublisher,
    AgentRunLiveStreamTerminalEvent,
)
from app.agent.running.daily_quota import observability as daily_quota_observability
from app.agent.running.deadline.deadline_exceeded import (
    DeadlineRunSweepResult,
    check_agent_run_deadline,
    sweep_deadline_exceeded_runs,
)
from app.log_policy.policies.research import RESEARCH_LOG_RULES
from app.log_policy.runtime import create_policy_json_logger

logger = create_policy_json_logger(__name__, RESEARCH_LOG_RULES)


async def sweep_and_report_deadline_exceeded_runs(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    live_redis: aioredis.Redis,
) -> None:
    async with session_factory() as session:
        async with session.begin():
            result = await sweep_deadline_exceeded_runs(session)
    await _report_deadline_recovery(live_redis, result)


async def check_and_report_run_deadline(
    run_id: UUID,
    *,
    session_factory: async_sessionmaker[AsyncSession],
    live_redis: aioredis.Redis,
) -> None:
    async with session_factory() as session:
        async with session.begin():
            result = await check_agent_run_deadline(session, run_id=run_id)
    await _report_deadline_recovery(live_redis, result)


async def _report_deadline_recovery(
    live_redis: aioredis.Redis, result: DeadlineRunSweepResult
) -> None:
    logger.info("agent_runs_deadline_swept", count=result.total_count)
    if result.queued_terminal_count > 0:
        logger.info(
            "agent_runs_queued_deadline_swept",
            run_count=result.queued_terminal_count,
            quota_released_count=result.queued_quota_released_count,
            quota_not_eligible_count=result.queued_quota_not_eligible_count,
            quota_inconsistent_count=result.queued_quota_inconsistent_count,
        )
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
    if result.running_terminal_runs:
        logger.info(
            "running_deadline_exceeded_swept",
            count=len(result.running_terminal_runs),
        )
    for running_run in result.running_terminal_runs:
        try:
            stream_events = AgentRunLiveStreamPublisher(
                live_redis,
                running_run.run_id,
                running_run.attempt_epoch,
            )
            await stream_events.publish(
                AgentRunLiveStreamTerminalEvent(
                    status="deadline_exceeded",
                ),
            )
        except Exception:
            logger.warning(
                "agent_run_live_stream_terminal_publish_failed",
                run_id=str(running_run.run_id),
                terminal_status="deadline_exceeded",
            )
