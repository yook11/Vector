"""Research async run API router."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncGenerator
from typing import Annotated
from uuid import UUID

import redis.asyncio as aioredis
import structlog
from fastapi import (
    APIRouter,
    Depends,
    Header,
    HTTPException,
    Query,
    Request,
    Response,
    status,
)
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.composition import ensure_question_answering_agent_configured
from app.agent.live_updates.recent_events import AgentRunLiveEventReader
from app.agent.live_updates.sse import (
    AgentRunQueuedSseConnection,
    AgentRunSseCapacity,
    AgentRunSsePreflightFailure,
    AgentRunSseTiming,
    prepare_running_sse_connection,
    validate_redis_stream_id,
)
from app.agent.live_updates.sse_response import AgentRunSseStreamingResponse
from app.agent.live_updates.stream import (
    AGENT_RUN_LIVE_STREAM_TIMEOUT_SECONDS,
    AgentRunLiveStreamPublisher,
    AgentRunLiveStreamReader,
    AgentRunLiveStreamTerminalEvent,
    agent_run_live_stream_key,
)
from app.agent.runs.contracts import (
    ActiveRunConflictError,
    CancelRunOutcome,
    OwnedAgentRunLiveContext,
    ThreadNotFoundError,
)
from app.agent.runs.repository import AgentRunRepository
from app.agent.runs.types import AgentRunErrorCode, AgentRunStatus
from app.agent.threads.repository import AgentThreadRepository
from app.analysis.ai_provider_errors import AIProviderError
from app.db import engine
from app.dependencies import CurrentUser, get_current_user, get_redis_client
from app.schemas.research import (
    PaginatedResearchThreadResponse,
    ResearchQuestionRequest,
    ResearchRunResponse,
    ResearchRunStartResponse,
    ResearchThreadDetail,
    ResearchThreadListParams,
)

router = APIRouter(prefix="/api/v1/research", tags=["research"])

logger = structlog.get_logger(__name__)

_GENERATION_UNAVAILABLE_DETAIL = "Answer generation is temporarily unavailable"
_ACTIVE_RUN_DETAIL = "A run is already in progress for this thread"
_RUN_ALREADY_COMPLETED_DETAIL = "Run already completed"
_THREAD_NOT_FOUND_DETAIL = "Research thread not found"
_RUN_NOT_FOUND_DETAIL = "Research run not found"
_SSE_RETRY_AFTER_SECONDS = 5
_SSE_CAPACITY_STATE_KEY = "agent_run_sse_capacity"


async def get_agent_persistence_session() -> AsyncGenerator[AsyncSession]:
    # get_session の request-wide UoW は commit→kiq→failed 更新の 2 tx 制御と分ける。
    async with AsyncSession(engine, expire_on_commit=False) as session:
        yield session


async def enqueue_agent_run(run_id: UUID) -> None:
    from app.queue.messages.agent_run import AgentRunTrigger
    from app.queue.tasks.agent_run import run_agent_answer

    await run_agent_answer.kiq(AgentRunTrigger(run_id=run_id))


async def read_agent_run_live_context(
    *,
    run_id: UUID,
    user_id: UUID,
) -> OwnedAgentRunLiveContext | None:
    async with AsyncSession(engine, expire_on_commit=False) as session:
        return await AgentRunRepository(session).read_live_context_for_user(
            run_id=run_id,
            user_id=user_id,
        )


def get_agent_run_sse_request_started_at() -> float:
    return time.monotonic()


def get_agent_run_sse_capacity(request: Request) -> AgentRunSseCapacity:
    capacity = getattr(request.app.state, _SSE_CAPACITY_STATE_KEY, None)
    if capacity is None:
        capacity = AgentRunSseCapacity()
        setattr(request.app.state, _SSE_CAPACITY_STATE_KEY, capacity)
    return capacity


def get_agent_run_sse_timing() -> AgentRunSseTiming:
    return AgentRunSseTiming()


@router.post(
    "/responses",
    operation_id="create_research_response",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=ResearchRunStartResponse,
    responses={
        status.HTTP_503_SERVICE_UNAVAILABLE: {
            "description": "Answer generation is temporarily unavailable"
        },
        status.HTTP_409_CONFLICT: {"description": _ACTIVE_RUN_DETAIL},
        status.HTTP_404_NOT_FOUND: {"description": _THREAD_NOT_FOUND_DETAIL},
    },
)
async def create_research_response(
    body: ResearchQuestionRequest,
    user: Annotated[CurrentUser, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_agent_persistence_session)],
) -> ResearchRunStartResponse:
    try:
        ensure_question_answering_agent_configured()
    except AIProviderError as exc:
        raise _generation_unavailable() from exc

    repo = AgentRunRepository(session)
    try:
        async with session.begin():
            created = await repo.create_user_run(
                user_id=user.id,
                question=body.question,
                thread_id=body.thread_id,
            )
    except ThreadNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND) from exc
    except ActiveRunConflictError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=_ACTIVE_RUN_DETAIL,
        ) from exc

    try:
        await enqueue_agent_run(created.run_id)
    except Exception as exc:
        logger.exception(
            "agent_run_enqueue_failed",
            run_id=str(created.run_id),
            error_type=exc.__class__.__name__,
        )
        try:
            async with session.begin():
                updated = await repo.mark_enqueue_failed(created.run_id)
                if not updated:
                    logger.info(
                        "agent_run_enqueue_failed_mark_failed_skipped",
                        run_id=str(created.run_id),
                    )
        except Exception as update_exc:
            logger.exception(
                "agent_run_enqueue_failed_mark_failed_failed",
                run_id=str(created.run_id),
                error_type=update_exc.__class__.__name__,
            )
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Failed to enqueue research run",
            ) from update_exc

    return ResearchRunStartResponse(thread_id=created.thread_id, run_id=created.run_id)


@router.get(
    "/threads",
    operation_id="list_research_threads",
    response_model=PaginatedResearchThreadResponse,
)
async def list_research_threads(
    pagination: Annotated[ResearchThreadListParams, Query()],
    user: Annotated[CurrentUser, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_agent_persistence_session)],
) -> PaginatedResearchThreadResponse:
    repo = AgentThreadRepository(session)
    return await repo.list_threads_for_user(user_id=user.id, pagination=pagination)


@router.get(
    "/threads/{thread_id}",
    operation_id="get_research_thread",
    response_model=ResearchThreadDetail,
    responses={
        status.HTTP_404_NOT_FOUND: {"description": _THREAD_NOT_FOUND_DETAIL},
    },
)
async def get_research_thread(
    thread_id: UUID,
    user: Annotated[CurrentUser, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_agent_persistence_session)],
) -> ResearchThreadDetail:
    repo = AgentThreadRepository(session)
    response = await repo.read_thread_detail_for_user(
        thread_id=thread_id,
        user_id=user.id,
    )
    if response is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    return response


@router.delete(
    "/threads/{thread_id}",
    operation_id="delete_research_thread",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={
        status.HTTP_404_NOT_FOUND: {"description": _THREAD_NOT_FOUND_DETAIL},
    },
)
async def delete_research_thread(
    thread_id: UUID,
    user: Annotated[CurrentUser, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_agent_persistence_session)],
) -> Response:
    repo = AgentThreadRepository(session)
    async with session.begin():
        deleted = await repo.delete_thread_for_user(
            thread_id=thread_id,
            user_id=user.id,
        )
    if not deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/runs/{run_id}/cancel",
    operation_id="cancel_research_run",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={
        status.HTTP_404_NOT_FOUND: {"description": _RUN_NOT_FOUND_DETAIL},
        status.HTTP_409_CONFLICT: {"description": _RUN_ALREADY_COMPLETED_DETAIL},
    },
)
async def cancel_research_run(
    run_id: UUID,
    user: Annotated[CurrentUser, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_agent_persistence_session)],
    redis: Annotated[aioredis.Redis, Depends(get_redis_client)],
) -> Response:
    repo = AgentRunRepository(session)
    async with session.begin():
        outcome = await repo.cancel_run_for_user(run_id=run_id, user_id=user.id)
    if outcome is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    if outcome.outcome is CancelRunOutcome.ALREADY_COMPLETED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=_RUN_ALREADY_COMPLETED_DETAIL,
        )
    if (
        outcome.outcome is CancelRunOutcome.CANCELLED
        and outcome.attempt_epoch is not None
        and outcome.attempt_epoch >= 1
    ):
        await _publish_cancel_terminal(
            redis=redis,
            run_id=run_id,
            attempt_epoch=outcome.attempt_epoch,
        )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


async def _publish_cancel_terminal(
    *,
    redis: aioredis.Redis,
    run_id: UUID,
    attempt_epoch: int,
) -> None:
    try:
        await AgentRunLiveStreamPublisher(
            redis,
            run_id,
            attempt_epoch,
        ).publish(
            AgentRunLiveStreamTerminalEvent(
                status="failed",
                errorCode=AgentRunErrorCode.CANCELLED,
            )
        )
    except Exception:
        logger.warning(
            "agent_run_cancel_terminal_publish_failed",
            run_id=str(run_id),
        )


@router.get(
    "/runs/{run_id}/events",
    operation_id="stream_research_run_events",
    response_class=StreamingResponse,
    responses={
        200: {
            "description": "SSE channel started",
            "content": {"text/event-stream": {}},
        },
        204: {"description": "Run is already terminal"},
        400: {"description": "Malformed run ID or Last-Event-ID"},
        401: {"description": "Not authenticated"},
        404: {"description": _RUN_NOT_FOUND_DETAIL},
        409: {"description": "The replay cursor was trimmed"},
        429: {"description": "Run or user connection limit exceeded"},
        503: {"description": "Live delivery is temporarily unavailable"},
    },
)
async def stream_research_run_events(
    run_id: str,
    request: Request,
    request_started_at: Annotated[
        float,
        Depends(get_agent_run_sse_request_started_at),
    ],
    user: Annotated[CurrentUser, Depends(get_current_user)],
    redis: Annotated[aioredis.Redis, Depends(get_redis_client)],
    capacity: Annotated[AgentRunSseCapacity, Depends(get_agent_run_sse_capacity)],
    timing: Annotated[AgentRunSseTiming, Depends(get_agent_run_sse_timing)],
    last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
) -> Response:
    parsed_run_id = _parse_sse_run_id(run_id)
    cursor = _parse_sse_cursor(last_event_id)
    lease = await capacity.try_acquire_process()
    if lease is None:
        return _sse_error_response(status.HTTP_503_SERVICE_UNAVAILABLE)
    try:
        context = await read_agent_run_live_context(
            run_id=parsed_run_id,
            user_id=user.id,
        )
        if context is None:
            await lease.release()
            return Response(
                status_code=status.HTTP_404_NOT_FOUND,
                headers={"Cache-Control": "no-store"},
            )
        if context.status in (AgentRunStatus.COMPLETED, AgentRunStatus.FAILED):
            await lease.release()
            return Response(
                status_code=status.HTTP_204_NO_CONTENT,
                headers={"Cache-Control": "no-store"},
            )
        rejection = await lease.try_acquire_owned(
            run_id=parsed_run_id,
            user_id=user.id,
        )
        if rejection is not None:
            return _sse_error_response(status.HTTP_429_TOO_MANY_REQUESTS)
        reader = AgentRunLiveStreamReader(redis)
        if context.status is AgentRunStatus.QUEUED and context.attempt_epoch == 0:
            try:
                await asyncio.wait_for(
                    redis.exists(agent_run_live_stream_key(parsed_run_id)),
                    timeout=AGENT_RUN_LIVE_STREAM_TIMEOUT_SECONDS,
                )
            except Exception:
                await lease.release()
                return _sse_error_response(status.HTTP_503_SERVICE_UNAVAILABLE)

            async def load_context() -> OwnedAgentRunLiveContext | None:
                return await read_agent_run_live_context(
                    run_id=parsed_run_id,
                    user_id=user.id,
                )

            connection = AgentRunQueuedSseConnection(
                run_id=parsed_run_id,
                cursor=cursor,
                reader=reader,
                lease=lease,
                load_context=load_context,
                timing=timing,
                started_at=request_started_at,
                clock=time.monotonic,
                sleep=asyncio.sleep,
                is_disconnected=request.is_disconnected,
            )
        else:
            if context.attempt_epoch < 1:
                await lease.release()
                return _sse_error_response(status.HTTP_503_SERVICE_UNAVAILABLE)
            prepared = await prepare_running_sse_connection(
                run_id=parsed_run_id,
                attempt_epoch=context.attempt_epoch,
                cursor=cursor,
                reader=reader,
                lease=lease,
                timing=timing,
                is_disconnected=request.is_disconnected,
                started_at=request_started_at,
            )
            if prepared is AgentRunSsePreflightFailure.CURSOR_TRIMMED:
                return _sse_error_response(status.HTTP_409_CONFLICT)
            if prepared is AgentRunSsePreflightFailure.UNAVAILABLE:
                return _sse_error_response(status.HTTP_503_SERVICE_UNAVAILABLE)
            connection = prepared
        return AgentRunSseStreamingResponse(
            connection.frames(),
            lease=lease,
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-store, no-transform",
                "X-Accel-Buffering": "no",
            },
        )
    except BaseException:
        await lease.release()
        raise


@router.get(
    "/runs/{run_id}",
    operation_id="get_research_run",
    response_model=ResearchRunResponse,
    responses={
        status.HTTP_404_NOT_FOUND: {"description": _RUN_NOT_FOUND_DETAIL},
    },
)
async def get_research_run(
    run_id: UUID,
    user: Annotated[CurrentUser, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_agent_persistence_session)],
    redis: Annotated[aioredis.Redis, Depends(get_redis_client)],
) -> ResearchRunResponse:
    repo = AgentRunRepository(session)
    response = await repo.read_run_for_user(run_id=run_id, user_id=user.id)
    if response is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    recent_events = await AgentRunLiveEventReader(redis).recent_events(run_id)
    return response.model_copy(update={"recent_events": recent_events})


def _generation_unavailable() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail=_GENERATION_UNAVAILABLE_DETAIL,
    )


def _parse_sse_run_id(value: str) -> UUID:
    try:
        parsed = UUID(value)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST) from exc
    if str(parsed) != value.lower():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST)
    return parsed


def _parse_sse_cursor(value: str | None) -> str | None:
    if value is None:
        return None
    try:
        return validate_redis_stream_id(value)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST) from exc


def _sse_error_response(status_code: int) -> Response:
    headers = {"Cache-Control": "no-store"}
    if status_code in (
        status.HTTP_429_TOO_MANY_REQUESTS,
        status.HTTP_503_SERVICE_UNAVAILABLE,
    ):
        headers["Retry-After"] = str(_SSE_RETRY_AFTER_SECONDS)
    return Response(
        status_code=status_code,
        headers=headers,
    )
