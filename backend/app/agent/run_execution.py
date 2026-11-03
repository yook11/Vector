"""workerによるRunの各回の実行。"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from uuid import UUID

import redis.asyncio as aioredis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent.answering.direct_answer.failure import DirectAnswerError
from app.agent.answering.evidence_answer.failure import EvidenceAnswerError
from app.agent.answering_runner import RunIdentity, RunInput
from app.agent.composition import build_answering_runner
from app.agent.contract import AnswerQuestionResult
from app.agent.live_updates.answer_delta import AgentRunLiveAnswerDeltaReporter
from app.agent.live_updates.reporters import (
    AgentRunLiveActivityReporter,
    AgentRunLiveStageReporter,
)
from app.agent.live_updates.stream import (
    AgentRunLiveStreamPublisher,
    AgentRunLiveStreamTerminalEvent,
)
from app.agent.planning.failure import PlanningError
from app.agent.research_handoff import (
    ResearchHandoff,
    recall_research_handoff,
)
from app.agent.running.answer_generation import (
    AgentAnswerGenerationRepository,
)
from app.agent.running.attempt_start import (
    AgentRunAttemptStartRepository,
    StartRunFailure,
    StartRunFailureReason,
)
from app.agent.running.completion import (
    AgentRunCompletionRepository,
    RunCompletionFailure,
    RunCompletionFailureReason,
    RunCompletionSuccess,
)
from app.agent.running.continuation import AgentRunExecutionProbe
from app.agent.running.failure_recording import AgentRunFailureRepository
from app.agent.running.question_reading import (
    AgentRunQuestionRepository,
    UserQuestionMessage,
)
from app.agent.running.stop import AnswerGenerationStopped, StopReason
from app.agent.running.task_boundary import AgentRunTaskBoundaryError
from app.agent.running.types import AgentRunErrorCode
from app.agent.runtime.contract import AgentResponseInvalidError
from app.agent.threads.contracts import ThreadMessageSnapshot
from app.agent.threads.history import HISTORY_MESSAGE_LIMIT, normalize_run_history
from app.agent.threads.repository import AgentThreadRepository
from app.ai_providers.errors import AIProviderError
from app.log_policy.policies.research import RESEARCH_LOG_RULES
from app.log_policy.runtime import create_policy_json_logger

logger = create_policy_json_logger(__name__, RESEARCH_LOG_RULES)


async def execute_run_attempt(
    run_id: UUID,
    *,
    session_factory: async_sessionmaker[AsyncSession],
    live_redis: aioredis.Redis,
    schedule_deadline_check: Callable[[UUID, datetime], None],
) -> None:
    attempt_epoch: int | None = None
    stream_events: AgentRunLiveStreamPublisher | None = None
    result: AnswerQuestionResult | None = None
    research_handoff: ResearchHandoff | None = None
    start_error: AgentRunTaskBoundaryError | None = None
    try:
        start_result = await _start_run(session_factory, run_id)
    except Exception as exc:
        logger.error(
            "agent_run_start_failed",
            exc_info=exc,
        )
        start_error = AgentRunTaskBoundaryError("agent run start failed")
    if start_error is not None:
        start_error.__suppress_context__ = True
        raise start_error
    if isinstance(start_result, int):
        attempt_epoch = start_result
    elif start_result.reason is StartRunFailureReason.DEADLINE_EXCEEDED:
        logger.info("agent_run_start_deadline_exceeded")
        return
    else:
        logger.info("agent_run_not_started", reason=start_result.reason.value)
        return

    question_row = None
    try:
        question_row = await _read_user_question(session_factory, run_id)
    except Exception as exc:
        logger.error(
            "agent_run_user_question_read_failed",
            run_id=str(run_id),
            exc_info=exc,
        )
    if question_row is None:
        await _mark_failed(
            session_factory,
            run_id,
            attempt_epoch,
            AgentRunErrorCode.INTERNAL_ERROR,
            None,
        )
        return
    user_id, thread_id, question = question_row

    stream_events = AgentRunLiveStreamPublisher(
        live_redis,
        run_id,
        attempt_epoch,
    )
    delta_reporter = AgentRunLiveAnswerDeltaReporter(
        stream_events,
        run_id=run_id,
        attempt_epoch=attempt_epoch,
    )
    continuation = AgentRunExecutionProbe(
        session_factory,
        run_id,
        attempt_epoch,
    )
    try:
        await stream_events.begin_attempt()
    except Exception:
        logger.warning(
            "agent_run_live_stream_begin_attempt_failed",
            run_id=str(run_id),
        )

    try:
        activity_reporter = AgentRunLiveActivityReporter(stream_events)
        progress_reporter = AgentRunLiveStageReporter(stream_events)
        as_of = datetime.now(UTC)
        history = await _read_history(
            session_factory,
            thread_id=thread_id,
            before_seq=question.seq,
        )
        research_handoff = await _read_research_handoff(
            session_factory,
            thread_id=thread_id,
            user_id=user_id,
            run_id=run_id,
        )
        answering_runner = build_answering_runner(
            schedule_deadline_check=schedule_deadline_check,
            session_factory=session_factory,
            repository=AgentAnswerGenerationRepository(
                session_factory,
                run_id,
                attempt_epoch,
            ),
            progress=progress_reporter,
            events=activity_reporter,
            delta_reporter=delta_reporter,
        )
        await continuation.ensure_can_continue()
        run_result = await answering_runner.run(
            RunInput(
                question=question.content,
                history=tuple(history),
                research_handoff=research_handoff,
            ),
            identity=RunIdentity(
                user_id=user_id,
                run_id=run_id,
                thread_id=thread_id,
                as_of=as_of,
            ),
        )
        result = run_result.final_output
        research_handoff = run_result.research_handoff
    except AnswerGenerationStopped as stopped:
        logger.info(
            "agent_run_generation_stopped",
            run_id=str(run_id),
            reason=stopped.reason.value,
        )
        if stopped.reason is StopReason.DEADLINE_EXCEEDED:
            await _publish_terminal(
                stream_events,
                run_id,
                AgentRunLiveStreamTerminalEvent(status="deadline_exceeded"),
            )
        return
    except (
        AIProviderError,
        AgentResponseInvalidError,
        DirectAnswerError,
        EvidenceAnswerError,
        PlanningError,
    ) as exc:
        logger.info(
            "agent_run_generation_unavailable",
            run_id=str(run_id),
            exc_info=exc,
        )
        await _mark_failed(
            session_factory,
            run_id,
            attempt_epoch,
            AgentRunErrorCode.GENERATION_UNAVAILABLE,
            stream_events,
        )
        return
    except Exception as exc:
        logger.error(
            "agent_run_unexpected_error",
            run_id=str(run_id),
            exc_info=exc,
        )
        await _mark_failed(
            session_factory,
            run_id,
            attempt_epoch,
            AgentRunErrorCode.INTERNAL_ERROR,
            stream_events,
        )
        return

    if attempt_epoch is None or stream_events is None or result is None:
        raise RuntimeError("completed run is missing its execution context")

    serialized_research_handoff = _serialize_research_handoff(
        research_handoff,
        run_id=run_id,
    )
    try:
        async with session_factory() as session:
            async with session.begin():
                completion_result = await AgentRunCompletionRepository(
                    session
                ).complete_run(
                    run_id=run_id,
                    result=result,
                    expected_attempt_epoch=attempt_epoch,
                    research_handoff=serialized_research_handoff,
                )
                if isinstance(completion_result, RunCompletionFailure):
                    logger.info(
                        "agent_run_completion_rejected",
                        run_id=str(run_id),
                        reason=completion_result.reason.value,
                    )
                    if (
                        completion_result.reason
                        is not RunCompletionFailureReason.DEADLINE_EXCEEDED
                    ):
                        await session.rollback()
        if isinstance(completion_result, RunCompletionSuccess):
            await _publish_terminal(
                stream_events,
                run_id,
                AgentRunLiveStreamTerminalEvent(status="completed"),
            )
        elif completion_result.reason is RunCompletionFailureReason.DEADLINE_EXCEEDED:
            await _publish_terminal(
                stream_events,
                run_id,
                AgentRunLiveStreamTerminalEvent(status="deadline_exceeded"),
            )
    except Exception:
        logger.exception(
            "agent_run_completion_failed",
            run_id=str(run_id),
        )
        await _mark_failed(
            session_factory,
            run_id,
            attempt_epoch,
            AgentRunErrorCode.INTERNAL_ERROR,
            stream_events,
        )


async def _start_run(
    session_factory: async_sessionmaker[AsyncSession],
    run_id: UUID,
) -> int | StartRunFailure:
    async with session_factory() as session:
        async with session.begin():
            return await AgentRunAttemptStartRepository(session).start_run(run_id)


def _serialize_research_handoff(
    research_handoff: ResearchHandoff | None,
    *,
    run_id: UUID,
) -> dict[str, object] | None:
    """handoffに起因しうる失敗をcomplete_run呼び出し前に完結させる。"""
    if research_handoff is None:
        return None
    try:
        return research_handoff.model_dump(mode="json")
    except Exception:
        logger.warning(
            "agent_run_research_handoff_serialization_failed",
            run_id=str(run_id),
            failure_code="serialization_failed",
        )
        return None


async def _read_user_question(
    session_factory: async_sessionmaker[AsyncSession],
    run_id: UUID,
) -> tuple[UUID, UUID, UserQuestionMessage] | None:
    async with session_factory() as session:
        return await AgentRunQuestionRepository(session).read_user_question_for_run(
            run_id
        )


async def _read_history(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    thread_id: UUID,
    before_seq: int,
) -> list[ThreadMessageSnapshot]:
    async with session_factory() as session:
        history = await AgentThreadRepository(session).read_recent_messages_before(
            thread_id=thread_id,
            before_seq=before_seq,
            limit=HISTORY_MESSAGE_LIMIT,
        )
    return normalize_run_history(history)


async def _read_research_handoff(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    thread_id: UUID,
    user_id: UUID,
    run_id: UUID,
) -> ResearchHandoff | None:
    """読出し・検証のどの失敗も握ってNoneにし、handoff無しのRunとして継続する。"""
    try:
        async with session_factory() as session:
            raw_handoff = await AgentThreadRepository(
                session
            ).read_research_handoff_for_user(
                thread_id=thread_id,
                user_id=user_id,
            )
        return recall_research_handoff(raw_handoff)
    except Exception as exc:
        logger.warning(
            "agent_run_research_handoff_read_failed",
            run_id=str(run_id),
            exc_info=exc,
            failure_code="research_handoff_read_failed",
        )
        return None


async def _mark_failed(
    session_factory: async_sessionmaker[AsyncSession],
    run_id: UUID,
    expected_attempt_epoch: int,
    error_code: AgentRunErrorCode,
    stream_events: AgentRunLiveStreamPublisher | None,
) -> bool:
    async with session_factory() as session:
        async with session.begin():
            transitioned = await AgentRunFailureRepository(session).mark_failed(
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
