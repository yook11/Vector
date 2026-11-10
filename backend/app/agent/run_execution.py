"""workerによるRunの各回の実行。"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from uuid import UUID

import redis.asyncio as aioredis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent.answering_runner import (
    AnsweringRunError,
    RunIdentity,
    RunInput,
    RunResult,
)
from app.agent.composition import build_answering_runner
from app.agent.live_updates.answer_delta import AgentRunLiveAnswerDeltaReporter
from app.agent.live_updates.reporters import (
    AgentRunLiveActivityReporter,
    AgentRunLiveStageReporter,
)
from app.agent.live_updates.stream import AgentRunLiveStreamPublisher
from app.agent.research_handoff import (
    ResearchHandoff,
    recall_research_handoff,
)
from app.agent.running.answer_generation import AgentRunAnswerGenerationGate
from app.agent.running.question_reading import (
    AgentRunQuestionRepository,
    UserQuestionMessage,
)
from app.agent.running.stage_gate import AgentRunStageGate
from app.agent.running.state import (
    AttemptStarted,
    AttemptSuperseded,
    CancelledByUser,
    CannotContinue,
    DeadlineExceeded,
    RunCompleted,
    RunNotFound,
)
from app.agent.running.state_service import AgentRunStateService
from app.agent.running.types import AgentRunErrorCode
from app.agent.threads.contracts import ThreadMessageSnapshot
from app.agent.threads.history import HISTORY_MESSAGE_LIMIT, normalize_run_history
from app.agent.threads.repository import AgentThreadRepository
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
    research_handoff: ResearchHandoff | None = None
    publishers: dict[tuple[UUID, int], AgentRunLiveStreamPublisher] = {}

    # 自分の世代の終了は、開始の印を送り済みの同じpublisherで通知する。
    def publisher_for(
        target_run_id: UUID, attempt_epoch: int
    ) -> AgentRunLiveStreamPublisher:
        key = (target_run_id, attempt_epoch)
        if key not in publishers:
            publishers[key] = AgentRunLiveStreamPublisher(
                live_redis, target_run_id, attempt_epoch
            )
        return publishers[key]

    state_service = AgentRunStateService(
        session_factory=session_factory, publisher_for=publisher_for
    )
    match await state_service.start_attempt(run_id):
        case AttemptStarted(attempt_epoch=attempt_epoch):
            pass
        case DeadlineExceeded():
            logger.info("agent_run_start_deadline_exceeded")
            return
        case CancelledByUser() | AttemptSuperseded() | RunNotFound() as stopped:
            logger.info("agent_run_not_started", reason=_stop_reason(stopped))
            return
    stream_events = publisher_for(run_id, attempt_epoch)

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
            state_service,
            run_id,
            attempt_epoch,
            AgentRunErrorCode.INTERNAL_ERROR,
        )
        return
    user_id, thread_id, question = question_row

    delta_reporter = AgentRunLiveAnswerDeltaReporter(
        stream_events,
        run_id=run_id,
        attempt_epoch=attempt_epoch,
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
        stage_reporter = AgentRunLiveStageReporter(stream_events)
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
            session_factory=session_factory,
            stage_gate=AgentRunStageGate(
                state_service,
                run_id,
                attempt_epoch,
                stage_reporter=stage_reporter,
            ),
            answer_generation_gate=AgentRunAnswerGenerationGate(
                state_service,
                run_id,
                attempt_epoch,
                schedule_deadline_check=schedule_deadline_check,
                stage_reporter=stage_reporter,
            ),
            events=activity_reporter,
            delta_reporter=delta_reporter,
        )
        run_outcome = await answering_runner.run(
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
    except AnsweringRunError as exc:
        logger.info(
            "agent_run_generation_unavailable",
            run_id=str(run_id),
            exc_info=exc,
        )
        await _mark_failed(
            state_service,
            run_id,
            attempt_epoch,
            AgentRunErrorCode.GENERATION_UNAVAILABLE,
        )
        return
    except Exception as exc:
        logger.error(
            "agent_run_unexpected_error",
            run_id=str(run_id),
            exc_info=exc,
        )
        await _mark_failed(
            state_service,
            run_id,
            attempt_epoch,
            AgentRunErrorCode.INTERNAL_ERROR,
        )
        return

    match run_outcome:
        case RunResult(final_output=result, research_handoff=research_handoff):
            pass
        case (
            CancelledByUser() | DeadlineExceeded() | AttemptSuperseded() | RunNotFound()
        ) as stopped:
            logger.info(
                "agent_run_generation_stopped",
                run_id=str(run_id),
                reason=_stop_reason(stopped),
            )
            return

    serialized_research_handoff = _serialize_research_handoff(
        research_handoff,
        run_id=run_id,
    )
    try:
        completion = await state_service.complete(
            run_id,
            attempt_epoch=attempt_epoch,
            result=result,
            research_handoff=serialized_research_handoff,
        )
    except Exception:
        logger.exception(
            "agent_run_completion_failed",
            run_id=str(run_id),
        )
        await _mark_failed(
            state_service,
            run_id,
            attempt_epoch,
            AgentRunErrorCode.INTERNAL_ERROR,
        )
        return
    match completion:
        case RunCompleted():
            return
        case (
            CancelledByUser() | DeadlineExceeded() | AttemptSuperseded() | RunNotFound()
        ) as stopped:
            logger.info(
                "agent_run_completion_rejected",
                run_id=str(run_id),
                reason=_stop_reason(stopped),
            )


def _stop_reason(stopped: CannotContinue) -> str:
    match stopped:
        case CancelledByUser():
            return "cancelled_by_user"
        case DeadlineExceeded():
            return "deadline_exceeded"
        case AttemptSuperseded():
            return "attempt_superseded"
        case RunNotFound():
            return "run_not_found"


def _serialize_research_handoff(
    research_handoff: ResearchHandoff | None,
    *,
    run_id: UUID,
) -> dict[str, object] | None:
    """handoffに起因しうる失敗を完了の保存より前に完結させる。"""
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
    state_service: AgentRunStateService,
    run_id: UUID,
    attempt_epoch: int,
    error_code: AgentRunErrorCode,
) -> None:
    # 既に止まっていたRunには書かず、閉じたときの通知は操作が送る。
    await state_service.record_failure(
        run_id,
        attempt_epoch=attempt_epoch,
        error_code=error_code,
    )
