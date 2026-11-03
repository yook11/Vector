"""Agent async run execution tasks."""

from __future__ import annotations

from taskiq import Context, TaskiqDepends

from app.agent.run_execution import execute_run_attempt
from app.agent.running.deadline.recovery import (
    check_and_report_run_deadline,
    sweep_and_report_deadline_exceeded_runs,
)
from app.queue.brokers import broker_agent
from app.queue.messages.agent_run import AgentRunTrigger
from app.queue.schedule import CRON_AGENT_RUN_SWEEP

RESEARCH_TASKIQ_TIMEOUT_SECONDS = 180


@broker_agent.task(
    task_name="run_agent_answer",
    timeout=RESEARCH_TASKIQ_TIMEOUT_SECONDS,
    max_retries=0,
    retry_on_error=False,
)
async def run_agent_answer(
    trigger: AgentRunTrigger,
    ctx: Context = TaskiqDepends(),
) -> None:
    await execute_run_attempt(
        trigger.run_id,
        session_factory=ctx.state.session_factory,
        live_redis=ctx.state.agent_live_redis,
        schedule_deadline_check=ctx.state.agent_deadline_scheduler.reserve_in_background,
    )


@broker_agent.task(
    task_name="sweep_deadline_exceeded_agent_runs",
    timeout=60,
    max_retries=0,
    retry_on_error=False,
    schedule=[{"cron": CRON_AGENT_RUN_SWEEP}],
)
async def sweep_deadline_exceeded_agent_runs(ctx: Context = TaskiqDepends()) -> None:
    await sweep_and_report_deadline_exceeded_runs(
        session_factory=ctx.state.session_factory,
        live_redis=ctx.state.agent_live_redis,
    )


@broker_agent.task(
    task_name="check_agent_run_deadline",
    timeout=60,
    max_retries=0,
    retry_on_error=False,
)
async def check_agent_run_deadline(
    trigger: AgentRunTrigger, ctx: Context = TaskiqDepends()
) -> None:
    await check_and_report_run_deadline(
        trigger.run_id,
        session_factory=ctx.state.session_factory,
        live_redis=ctx.state.agent_live_redis,
    )
