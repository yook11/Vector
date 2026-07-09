"""Agent async run execution tasks."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from taskiq import Context, TaskiqDepends

from app.agent.answering.direct import DirectAnswerInvalidError
from app.agent.composition import (
    build_question_answering_agent,
    build_question_resolver,
)
from app.agent.contract import AnswerQuestionInput, QuestionResolvedEvent
from app.agent.history import (
    AgentHistoryRepository,
    AgentRunErrorCode,
    PreparedAgentRun,
    RunTransitionLostError,
    ThreadMessageSnapshot,
)
from app.agent.history.live_events import AgentRunLiveEventPublisher
from app.agent.history.progress import AgentRunProgressWriter
from app.agent.question_resolution.service import (
    HISTORY_MESSAGE_LIMIT,
    QuestionResolutionService,
)
from app.analysis.ai_provider_errors import (
    AIProviderConfigurationError,
    AIProviderError,
)
from app.queue.brokers import broker_agent
from app.queue.messages.agent_run import AgentRunTrigger
from app.queue.schedule import CRON_AGENT_RUN_SWEEP
from app.redis import get_redis
from app.shared.security.safe_http import make_safe_async_client

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
    events = AgentRunLiveEventPublisher(get_redis(), prepared.run_id)

    try:
        await events.reset()
        as_of = datetime.now(UTC)
        history = await _read_history(session_factory, prepared)
        resolver = build_question_resolver() if history else None
        resolved = await QuestionResolutionService(resolver=resolver).resolve(
            question=prepared.question,
            history=history,
            as_of=as_of,
            run_id=prepared.run_id,
        )
        if resolved.standalone_question.strip() != prepared.question.strip():
            await events.event_occurred(
                QuestionResolvedEvent(
                    standalone_question=resolved.standalone_question,
                )
            )
        async with make_safe_async_client() as tavily_client:
            agent = build_question_answering_agent(
                session_factory=session_factory,
                tavily_client=tavily_client,
                progress=AgentRunProgressWriter(
                    session_factory,
                    prepared.run_id,
                ),
                events=events,
            )
            result = await agent.answer(
                AnswerQuestionInput(
                    question=resolved.standalone_question,
                    as_of=as_of,
                    user_intent=resolved.user_intent,
                    prior_coverage=resolved.prior_coverage,
                    user_activity_context=resolved.user_activity_context,
                    previous_answer=_latest_assistant_answer(history),
                )
            )
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
            AgentRunErrorCode.GENERATION_UNAVAILABLE,
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
            AgentRunErrorCode.INTERNAL_ERROR,
        )
        return

    try:
        async with session_factory() as session:
            async with session.begin():
                completed = await AgentHistoryRepository(session).complete_run(
                    run_id=prepared.run_id,
                    result=result,
                )
                if not completed:
                    logger.info(
                        "agent_run_completion_skipped",
                        run_id=str(prepared.run_id),
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
            AgentRunErrorCode.INTERNAL_ERROR,
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
            count = await AgentHistoryRepository(session).sweep_stale_runs()
    logger.info("agent_runs_stale_swept", count=count)


async def _acquire_run(
    session_factory: async_sessionmaker[AsyncSession],
    trigger: AgentRunTrigger,
) -> PreparedAgentRun | None:
    async with session_factory() as session:
        async with session.begin():
            return await AgentHistoryRepository(session).acquire_for_execution(
                trigger.run_id
            )


async def _read_history(
    session_factory: async_sessionmaker[AsyncSession],
    prepared: PreparedAgentRun,
) -> list[ThreadMessageSnapshot]:
    async with session_factory() as session:
        return await AgentHistoryRepository(session).read_recent_messages_before(
            thread_id=prepared.thread_id,
            before_seq=prepared.user_message_seq,
            limit=HISTORY_MESSAGE_LIMIT,
        )


def _latest_assistant_answer(history: list[ThreadMessageSnapshot]) -> str:
    for message in reversed(history):
        if message.role == "assistant":
            return message.content
    return ""


async def _mark_failed(
    session_factory: async_sessionmaker[AsyncSession],
    run_id: UUID,
    error_code: AgentRunErrorCode,
) -> None:
    async with session_factory() as session:
        async with session.begin():
            await AgentHistoryRepository(session).mark_failed(
                run_id,
                error_code=error_code,
            )
