"""Agent async run execution tasks."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from taskiq import Context, TaskiqDepends

from app.agent.answering.direct_answer.contract import DirectAnswerInvalidError
from app.agent.composition import (
    build_answering_runner,
    build_question_answering_starting_agent,
)
from app.agent.contract import AnswerGenerationStopped
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
    PreparedAgentRun,
    RunTransitionLostError,
)
from app.agent.runs.execution_probe import AgentRunExecutionProbe
from app.agent.runs.progress import AgentRunProgressWriter
from app.agent.runs.repository import AgentRunRepository
from app.agent.runs.types import AgentRunErrorCode
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


@broker_agent.task(
    task_name="run_agent_answer",
    timeout=300,
    max_retries=0,
    retry_on_error=False,
)
async def run_agent_answer(
    trigger: AgentRunTrigger,
    ctx: Context = TaskiqDepends(),
) -> None:
    session_factory = ctx.state.session_factory
    prepared = await _acquire_run(session_factory, trigger)
    if prepared is None:
        logger.info("agent_run_idempotent_skip", run_id=str(trigger.run_id))
        return
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
        answering_runner = build_answering_runner()
        starting_agent = build_question_answering_starting_agent(
            session_factory=session_factory,
            progress=progress_reporter,
            events=activity_reporter,
            delta_reporter=delta_reporter,
            continuation=continuation,
        )
        run_result = await answering_runner.run(
            starting_agent,
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
    except AnswerGenerationStopped:
        logger.info(
            "agent_run_generation_stopped",
            run_id=str(prepared.run_id),
        )
        return
    except (
        AIProviderConfigurationError,
        AIProviderError,
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
        logger.exception(
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
    async with session_factory() as session:
        async with session.begin():
            count = await AgentRunRepository(session).sweep_stale_runs()
    logger.info("agent_runs_stale_swept", count=count)


async def _acquire_run(
    session_factory: async_sessionmaker[AsyncSession],
    trigger: AgentRunTrigger,
) -> PreparedAgentRun | None:
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
    stream_events: AgentRunLiveStreamPublisher,
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
    await _publish_terminal(
        stream_events,
        run_id,
        AgentRunLiveStreamTerminalEvent(
            status="failed",
            errorCode=error_code,
        ),
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
