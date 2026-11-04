"""失敗は再試行せず方針つきloggerに記録して終え、取り残されたRunは期限切れの回収タスクが確定させる。"""

from __future__ import annotations

from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode
from taskiq import Context, TaskiqDepends

from app.agent.run_enqueuer import AgentRunTrigger
from app.agent.run_execution import execute_run_attempt
from app.agent.running.deadline.recovery import (
    check_and_report_run_deadline,
    sweep_and_report_deadline_exceeded_runs,
)
from app.log_policy.policies.research import RESEARCH_LOG_RULES
from app.log_policy.runtime import create_policy_json_logger
from app.queue.brokers import broker_agent
from app.queue.schedule import CRON_AGENT_RUN_SWEEP

logger = create_policy_json_logger(__name__, RESEARCH_LOG_RULES)

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
    # taskiqは引数を解釈できないと元の値のまま渡すので、ここで型を確かめる。
    if not isinstance(trigger, AgentRunTrigger):
        _mark_execute_span_failed()
        logger.error("agent_run_trigger_invalid")
        return
    try:
        await execute_run_attempt(
            trigger.run_id,
            session_factory=ctx.state.session_factory,
            live_redis=ctx.state.agent_live_redis,
            schedule_deadline_check=ctx.state.agent_deadline_scheduler.reserve_in_background,
        )
    except Exception as exc:
        _mark_execute_span_failed(exc)
        logger.error("agent_run_task_failed", run_id=str(trigger.run_id), exc_info=exc)


@broker_agent.task(
    task_name="sweep_deadline_exceeded_agent_runs",
    timeout=60,
    max_retries=0,
    retry_on_error=False,
    schedule=[{"cron": CRON_AGENT_RUN_SWEEP}],
)
async def sweep_deadline_exceeded_agent_runs(ctx: Context = TaskiqDepends()) -> None:
    try:
        await sweep_and_report_deadline_exceeded_runs(
            session_factory=ctx.state.session_factory,
            live_redis=ctx.state.agent_live_redis,
        )
    except Exception as exc:
        _mark_execute_span_failed(exc)
        logger.error("agent_run_deadline_sweep_failed", exc_info=exc)


@broker_agent.task(
    task_name="check_agent_run_deadline",
    timeout=60,
    max_retries=0,
    retry_on_error=False,
)
async def check_agent_run_deadline(
    trigger: AgentRunTrigger, ctx: Context = TaskiqDepends()
) -> None:
    # taskiqは引数を解釈できないと元の値のまま渡すので、ここで型を確かめる。
    if not isinstance(trigger, AgentRunTrigger):
        _mark_execute_span_failed()
        logger.error("agent_run_trigger_invalid")
        return
    try:
        await check_and_report_run_deadline(
            trigger.run_id,
            session_factory=ctx.state.session_factory,
            live_redis=ctx.state.agent_live_redis,
        )
    except Exception as exc:
        _mark_execute_span_failed(exc)
        logger.error(
            "agent_run_deadline_check_failed",
            run_id=str(trigger.run_id),
            exc_info=exc,
        )


def _mark_execute_span_failed(exc: Exception | None = None) -> None:
    # 例外をtaskiqへ渡さないので、実行のspanを失敗にする印はここで付ける。
    span = trace.get_current_span()
    if exc is not None:
        span.record_exception(exc)
    span.set_status(Status(StatusCode.ERROR))
