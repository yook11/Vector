"""Agent async run execution tasks."""

from __future__ import annotations

import asyncio
import time
from contextlib import suppress
from datetime import UTC, datetime
from uuid import UUID

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from taskiq import Context, TaskiqDepends

from app.agent.answering.direct_answer.contract import DirectAnswerInvalidError
from app.agent.composition import build_answering_runner
from app.agent.contract import AnswerGenerationStopped, AnswerQuestionResult
from app.agent.input_safety.contract import InputSafetyBlocked
from app.agent.live_updates.answer_delta import AgentRunLiveAnswerDeltaReporter
from app.agent.live_updates.recent_events import AgentRunLiveEventPublisher
from app.agent.live_updates.reporters import (
    AgentRunLiveActivityReporter,
    AgentRunLiveStageReporter,
)
from app.agent.live_updates.stream import (
    AgentRunLiveStreamPublisher,
    AgentRunLiveStreamTerminalEvent,
)
from app.agent.question_context.service import HISTORY_MESSAGE_LIMIT
from app.agent.running import (
    QuestionResolvedRunHooks,
    RunContext,
    RunInput,
)
from app.agent.runs.contracts import (
    AcquireForExecutionCommandOutcome,
    AcquireForExecutionOutcome,
    PreparedAgentRun,
    RunTransitionLostError,
)
from app.agent.runs.daily_quota import observability as daily_quota_observability
from app.agent.runs.execution_probe import AgentRunExecutionProbe
from app.agent.runs.progress import AgentRunProgressWriter
from app.agent.runs.repository import AgentRunRepository
from app.agent.runs.types import AgentRunErrorCode
from app.agent.runtime.contract import AgentResponseInvalidError
from app.agent.threads.contracts import ThreadMessageSnapshot
from app.agent.threads.repository import AgentThreadRepository
from app.analysis.ai_provider_errors import (
    AIProviderConfigurationError,
    AIProviderError,
)
from app.queue.brokers import broker_agent
from app.queue.messages.agent_run import AgentRunTrigger
from app.queue.schedule import CRON_AGENT_RUN_SWEEP
from app.redis import get_redis

logger = structlog.get_logger(__name__)

RESEARCH_APPLICATION_TIMEOUT_SECONDS = 150
RESEARCH_TASKIQ_TIMEOUT_SECONDS = 180


class AgentRunTaskBoundaryError(RuntimeError):
    """Agent run task boundaryで公開する安全な永続化エラー。"""


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
    session_factory = ctx.state.session_factory
    prepared: PreparedAgentRun | None = None
    stream_events: AgentRunLiveStreamPublisher | None = None
    result: AnswerQuestionResult | None = None
    application_deadline_at = time.monotonic() + RESEARCH_APPLICATION_TIMEOUT_SECONDS
    application_deadline = asyncio.timeout_at(application_deadline_at)
    application_deadline_reached_after_acquire = False
    timeout_terminalization_error: AgentRunTaskBoundaryError | None = None
    try:
        async with application_deadline:
            acquisition_error: AgentRunTaskBoundaryError | None = None
            try:
                acquire_result = await _acquire_run(session_factory, trigger)
            except Exception as exc:
                logger.error(
                    "agent_run_acquisition_failed",
                    error_type=exc.__class__.__name__,
                )
                acquisition_error = AgentRunTaskBoundaryError(
                    "agent run acquisition failed"
                )
            if acquisition_error is not None:
                acquisition_error.__suppress_context__ = True
                raise acquisition_error
            if (
                acquire_result.acquire_outcome
                is AcquireForExecutionOutcome.QUEUED_START_DEADLINE_EXPIRED
            ):
                quota_release_outcome = acquire_result.quota_release_outcome
                if quota_release_outcome is None:
                    raise RuntimeError(
                        "queued expiry is missing its quota release outcome"
                    )
                logger.info(
                    "agent_run_queued_start_deadline_expired",
                    quota_release_result=quota_release_outcome.value,
                )
                with suppress(Exception):
                    daily_quota_observability.observe_release(
                        run_id=trigger.run_id,
                        outcome=quota_release_outcome,
                    )
                return
            if (
                acquire_result.acquire_outcome
                is AcquireForExecutionOutcome.IDEMPOTENT_SKIP
            ):
                logger.info("agent_run_idempotent_skip", run_id=str(trigger.run_id))
                return
            prepared = acquire_result.prepared_run
            if prepared is None:
                raise RuntimeError("acquired run is missing its prepared run")
            if time.monotonic() >= application_deadline_at:
                application_deadline_reached_after_acquire = True
                raise TimeoutError

            redis = get_redis()
            events = AgentRunLiveEventPublisher(redis, prepared.run_id)
            stream_events = AgentRunLiveStreamPublisher(
                redis,
                prepared.run_id,
                prepared.attempt_epoch,
            )
            delta_reporter = AgentRunLiveAnswerDeltaReporter(
                stream_events,
                run_id=prepared.run_id,
                attempt_epoch=prepared.attempt_epoch,
            )
            continuation = AgentRunExecutionProbe(
                session_factory,
                prepared.run_id,
                prepared.attempt_epoch,
            )
            try:
                await stream_events.begin_attempt()
            except Exception:
                logger.warning(
                    "agent_run_live_stream_begin_attempt_failed",
                    run_id=str(prepared.run_id),
                )

            try:
                await events.reset()
                activity_reporter = AgentRunLiveActivityReporter(events, stream_events)
                progress_reporter = AgentRunLiveStageReporter(
                    AgentRunProgressWriter(
                        session_factory,
                        prepared.run_id,
                        prepared.attempt_epoch,
                    ),
                    stream_events,
                )
                as_of = datetime.now(UTC)
                history = await _read_history(session_factory, prepared)
                answering_runner = build_answering_runner(
                    session_factory=session_factory,
                    progress=progress_reporter,
                    events=activity_reporter,
                    delta_reporter=delta_reporter,
                    continuation=continuation,
                )
                run_result = await answering_runner.run(
                    RunInput(
                        question=prepared.question,
                        history=tuple(history),
                    ),
                    run_context=RunContext(
                        run_id=prepared.run_id,
                        as_of=as_of,
                    ),
                    hooks=QuestionResolvedRunHooks(events=activity_reporter),
                )
                result = run_result.final_output
            except InputSafetyBlocked:
                await _mark_policy_blocked(
                    session_factory,
                    prepared.run_id,
                    prepared.attempt_epoch,
                    stream_events,
                )
                return
            except AnswerGenerationStopped:
                logger.info(
                    "agent_run_generation_stopped",
                    run_id=str(prepared.run_id),
                )
                return
            except (
                AIProviderConfigurationError,
                AIProviderError,
                AgentResponseInvalidError,
                DirectAnswerInvalidError,
            ) as exc:
                logger.info(
                    "agent_run_generation_unavailable",
                    run_id=str(prepared.run_id),
                    error_type=exc.__class__.__name__,
                )
                await _mark_failed(
                    session_factory,
                    prepared.run_id,
                    prepared.attempt_epoch,
                    AgentRunErrorCode.GENERATION_UNAVAILABLE,
                    stream_events,
                )
                return
            except Exception as exc:
                logger.error(
                    "agent_run_unexpected_error",
                    run_id=str(prepared.run_id),
                    error_type=exc.__class__.__name__,
                )
                await _mark_failed(
                    session_factory,
                    prepared.run_id,
                    prepared.attempt_epoch,
                    AgentRunErrorCode.INTERNAL_ERROR,
                    stream_events,
                )
                return
    except TimeoutError:
        if not (
            application_deadline_reached_after_acquire or application_deadline.expired()
        ):
            raise
        if prepared is None:
            logger.info("application_timeout_without_attempt")
            return
        if stream_events is None:
            try:
                redis = get_redis()
                stream_events = AgentRunLiveStreamPublisher(
                    redis,
                    prepared.run_id,
                    prepared.attempt_epoch,
                )
            except Exception:
                logger.warning(
                    "agent_run_live_stream_timeout_publisher_init_failed",
                    run_id=str(prepared.run_id),
                )
        try:
            transitioned = await _mark_failed(
                session_factory,
                prepared.run_id,
                prepared.attempt_epoch,
                AgentRunErrorCode.GENERATION_UNAVAILABLE,
                stream_events,
            )
        except Exception as exc:
            logger.error(
                "application_timeout_terminalize_failed",
                error_type=exc.__class__.__name__,
            )
            timeout_terminalization_error = AgentRunTaskBoundaryError(
                "agent run timeout terminalization failed"
            )
        if timeout_terminalization_error is None:
            if transitioned:
                logger.info(
                    "application_timeout_terminalized",
                    error_code=AgentRunErrorCode.GENERATION_UNAVAILABLE.value,
                    attempt_outcome="terminalized",
                )
            else:
                logger.info(
                    "application_timeout_lost_race",
                    attempt_outcome="lost_race",
                )
            return

    if timeout_terminalization_error is not None:
        timeout_terminalization_error.__suppress_context__ = True
        raise timeout_terminalization_error

    if prepared is None or stream_events is None or result is None:
        raise RuntimeError("completed run is missing its execution context")

    try:
        async with session_factory() as session:
            async with session.begin():
                completed = await AgentRunRepository(session).complete_run(
                    run_id=prepared.run_id,
                    result=result,
                    expected_attempt_epoch=prepared.attempt_epoch,
                )
                if not completed:
                    logger.info(
                        "agent_run_completion_skipped",
                        run_id=str(prepared.run_id),
                    )
        if completed:
            await _publish_terminal(
                stream_events,
                prepared.run_id,
                AgentRunLiveStreamTerminalEvent(status="completed"),
            )
    except RunTransitionLostError:
        logger.info("agent_run_completion_lost_race", run_id=str(prepared.run_id))
    except Exception as exc:
        logger.exception(
            "agent_run_completion_failed",
            run_id=str(prepared.run_id),
            error_type=exc.__class__.__name__,
        )
        await _mark_failed(
            session_factory,
            prepared.run_id,
            prepared.attempt_epoch,
            AgentRunErrorCode.INTERNAL_ERROR,
            stream_events,
        )


@broker_agent.task(
    task_name="sweep_stale_agent_runs",
    timeout=60,
    max_retries=0,
    retry_on_error=False,
    schedule=[{"cron": CRON_AGENT_RUN_SWEEP}],
)
async def sweep_stale_agent_runs(ctx: Context = TaskiqDepends()) -> None:
    session_factory = ctx.state.session_factory
    sweep_error: AgentRunTaskBoundaryError | None = None
    try:
        async with session_factory() as session:
            async with session.begin():
                result = await AgentRunRepository(session).sweep_stale_runs()
    except Exception as exc:
        logger.error(
            "agent_run_stale_sweep_failed",
            error_type=exc.__class__.__name__,
        )
        sweep_error = AgentRunTaskBoundaryError("agent run stale sweep failed")
    if sweep_error is not None:
        sweep_error.__suppress_context__ = True
        raise sweep_error
    with suppress(Exception):
        logger.info("agent_runs_stale_swept", count=result.total_count)
    if result.queued_terminal_count > 0:
        with suppress(Exception):
            logger.info(
                "agent_runs_queued_stale_swept",
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
        with suppress(Exception):
            logger.info(
                "running_timeout_swept",
                count=len(result.running_terminal_runs),
            )
    if result.running_without_started_at_count > 0:
        with suppress(Exception):
            logger.warning(
                "running_without_started_at",
                count=result.running_without_started_at_count,
            )
    for running_run in result.running_terminal_runs:
        try:
            redis = get_redis()
            stream_events = AgentRunLiveStreamPublisher(
                redis,
                running_run.run_id,
                running_run.attempt_epoch,
            )
            await _publish_terminal(
                stream_events,
                running_run.run_id,
                AgentRunLiveStreamTerminalEvent(
                    status="failed",
                    errorCode=AgentRunErrorCode.STALE,
                ),
            )
        except Exception:
            logger.warning(
                "agent_run_live_stream_terminal_publish_failed",
                run_id=str(running_run.run_id),
                terminal_status="failed",
            )


async def _acquire_run(
    session_factory: async_sessionmaker[AsyncSession],
    trigger: AgentRunTrigger,
) -> AcquireForExecutionCommandOutcome:
    async with session_factory() as session:
        async with session.begin():
            return await AgentRunRepository(session).acquire_for_execution(
                trigger.run_id
            )


async def _read_history(
    session_factory: async_sessionmaker[AsyncSession],
    prepared: PreparedAgentRun,
) -> list[ThreadMessageSnapshot]:
    async with session_factory() as session:
        return await AgentThreadRepository(session).read_recent_messages_before(
            thread_id=prepared.thread_id,
            before_seq=prepared.user_message_seq,
            limit=HISTORY_MESSAGE_LIMIT,
        )


async def _mark_failed(
    session_factory: async_sessionmaker[AsyncSession],
    run_id: UUID,
    expected_attempt_epoch: int,
    error_code: AgentRunErrorCode,
    stream_events: AgentRunLiveStreamPublisher | None,
) -> bool:
    async with session_factory() as session:
        async with session.begin():
            transitioned = await AgentRunRepository(session).mark_failed(
                run_id,
                expected_attempt_epoch=expected_attempt_epoch,
                error_code=error_code,
            )
    if not transitioned:
        return False
    if stream_events is not None:
        await _publish_terminal(
            stream_events,
            run_id,
            AgentRunLiveStreamTerminalEvent(
                status="failed",
                errorCode=error_code,
            ),
        )
    return True


async def _mark_policy_blocked(
    session_factory: async_sessionmaker[AsyncSession],
    run_id: UUID,
    expected_attempt_epoch: int,
    stream_events: AgentRunLiveStreamPublisher,
) -> bool:
    async with session_factory() as session:
        async with session.begin():
            transitioned = await AgentRunRepository(session).mark_policy_blocked(
                run_id,
                expected_attempt_epoch=expected_attempt_epoch,
            )
    if not transitioned:
        return False
    await _publish_terminal(
        stream_events,
        run_id,
        AgentRunLiveStreamTerminalEvent(status="policy_blocked"),
    )
    return True


async def _publish_terminal(
    stream_events: AgentRunLiveStreamPublisher,
    run_id: UUID,
    event: AgentRunLiveStreamTerminalEvent,
) -> None:
    try:
        await stream_events.publish(event)
    except Exception:
        logger.warning(
            "agent_run_live_stream_terminal_publish_failed",
            run_id=str(run_id),
            terminal_status=event.status,
        )
