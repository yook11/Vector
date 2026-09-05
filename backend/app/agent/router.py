"""Research async run API router."""

from __future__ import annotations

import asyncio
import math
import time
from contextlib import suppress
from datetime import datetime, timedelta
from typing import Annotated
from uuid import UUID

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
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent.composition import ensure_external_search_configured
from app.agent.daily_quota import observability as daily_quota_observability
from app.agent.daily_quota.contracts import DailyRequestLimitExceededError
from app.agent.daily_quota.policy import DAILY_QUOTA_TIMEZONE
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
    AgentRunLiveStreamTerminalEvent,
)
from app.agent.live_updates.transport import (
    AgentLiveTransport,
    get_agent_live_transport,
)
from app.agent.runs.contracts import (
    ActiveRunConflictError,
    CancelRunOutcome,
    OwnedAgentRunLiveContext,
    ThreadNotFoundError,
)
from app.agent.runs.enqueuer import AgentRunEnqueuer, get_agent_run_enqueuer
from app.agent.runs.repository import AgentRunRepository
from app.agent.runs.types import AgentRunErrorCode, AgentRunStatus
from app.agent.threads.detail import read_owned_thread_detail
from app.agent.threads.repository import AgentThreadRepository
from app.analysis.ai_provider_errors import AIProviderError
from app.db.fastapi import get_caller_managed_session
from app.dependencies import (
    CurrentUser,
    get_current_user,
)
from app.schemas.research import (
    PaginatedResearchThreadResponse,
    ResearchDailyRequestLimitExceededResponse,
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


async def read_agent_run_live_context(
    *,
    run_id: UUID,
    user_id: UUID,
    session_factory: async_sessionmaker[AsyncSession],
) -> OwnedAgentRunLiveContext | None:
    async with session_factory() as session:
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
        status.HTTP_429_TOO_MANY_REQUESTS: {
            "description": "Daily research request limit exceeded",
            "model": ResearchDailyRequestLimitExceededResponse,
        },
    },
)
async def create_research_response(
    body: ResearchQuestionRequest,
    user: Annotated[CurrentUser, Depends(get_current_user)],
    # commit→kiq→failed の 2 tx を切るため、入口管理の UoW は使わない。
    session: Annotated[AsyncSession, Depends(get_caller_managed_session)],
    enqueuer: Annotated[AgentRunEnqueuer, Depends(get_agent_run_enqueuer)],
) -> ResearchRunStartResponse | JSONResponse:
    try:
        ensure_external_search_configured()
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
    except DailyRequestLimitExceededError as exc:
        with suppress(Exception):
            daily_quota_observability.observe_admission_rejected(
                usage_date=exc.usage_date,
            )
        reset_at = datetime.combine(
            exc.usage_date + timedelta(days=1),
            datetime.min.time(),
            tzinfo=DAILY_QUOTA_TIMEZONE,
        )
        retry_after = max(
            0,
            math.ceil((reset_at - exc.decided_at).total_seconds()),
        )
        response = ResearchDailyRequestLimitExceededResponse(
            detail="Daily research request limit exceeded",
            code="research_daily_request_limit_exceeded",
            limit=exc.limit,
            reset_at=reset_at,
        )
        return JSONResponse(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            content=response.model_dump(mode="json", by_alias=True),
            headers={
                "Retry-After": str(retry_after),
                "Cache-Control": "no-store",
            },
        )

    with suppress(Exception):
        daily_quota_observability.observe_admission_accepted(
            run_id=created.run_id,
            usage_date=created.usage_date,
            used_count=created.used_count,
        )

    try:
        await enqueuer.enqueue(created.run_id)
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
    session: Annotated[AsyncSession, Depends(get_caller_managed_session)],
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
    session: Annotated[AsyncSession, Depends(get_caller_managed_session)],
    live: Annotated[AgentLiveTransport, Depends(get_agent_live_transport)],
) -> ResearchThreadDetail:
    response = await read_owned_thread_detail(
        session,
        thread_id=thread_id,
        user_id=user.id,
        live=live,
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
    session: Annotated[AsyncSession, Depends(get_caller_managed_session)],
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
    session: Annotated[AsyncSession, Depends(get_caller_managed_session)],
    live: Annotated[AgentLiveTransport, Depends(get_agent_live_transport)],
) -> Response:
    repo = AgentRunRepository(session)
    async with session.begin():
        outcome = await repo.cancel_run_for_user(run_id=run_id, user_id=user.id)
    if outcome is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    if outcome.cancel_outcome is CancelRunOutcome.ALREADY_COMPLETED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=_RUN_ALREADY_COMPLETED_DETAIL,
        )
    if outcome.cancel_outcome is CancelRunOutcome.CANCELLED:
        if outcome.quota_release_outcome is not None:
            with suppress(Exception):
                daily_quota_observability.observe_release(
                    run_id=run_id,
                    outcome=outcome.quota_release_outcome,
                )
        running_attempt_epoch = outcome.running_attempt_epoch
        if outcome.was_running and running_attempt_epoch is None:
            raise RuntimeError("running cancel outcome is missing its attempt epoch")
    else:
        running_attempt_epoch = None
    if outcome.was_running and running_attempt_epoch is not None:
        await _publish_cancel_terminal(
            live=live,
            run_id=run_id,
            attempt_epoch=running_attempt_epoch,
        )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


async def _publish_cancel_terminal(
    *,
    live: AgentLiveTransport,
    run_id: UUID,
    attempt_epoch: int,
) -> None:
    try:
        await live.publisher(run_id, attempt_epoch).publish(
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
    live: Annotated[AgentLiveTransport, Depends(get_agent_live_transport)],
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
        session_factory = request.app.state.session_factory
        context = await read_agent_run_live_context(
            run_id=parsed_run_id,
            user_id=user.id,
            session_factory=session_factory,
        )
        if context is None:
            await lease.release()
            return Response(
                status_code=status.HTTP_404_NOT_FOUND,
                headers={"Cache-Control": "no-store"},
            )
        if context.status in (
            AgentRunStatus.COMPLETED,
            AgentRunStatus.POLICY_BLOCKED,
            AgentRunStatus.DEADLINE_EXCEEDED,
            AgentRunStatus.FAILED,
        ):
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
        reader = live.reader()
        if context.status is AgentRunStatus.QUEUED and context.attempt_epoch == 0:
            try:
                await asyncio.wait_for(
                    live.exists(parsed_run_id),
                    timeout=AGENT_RUN_LIVE_STREAM_TIMEOUT_SECONDS,
                )
            except Exception:
                await lease.release()
                return _sse_error_response(status.HTTP_503_SERVICE_UNAVAILABLE)

            async def load_context() -> OwnedAgentRunLiveContext | None:
                return await read_agent_run_live_context(
                    run_id=parsed_run_id,
                    user_id=user.id,
                    session_factory=session_factory,
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
    session: Annotated[AsyncSession, Depends(get_caller_managed_session)],
) -> ResearchRunResponse:
    repo = AgentRunRepository(session)
    response = await repo.read_run_for_user(run_id=run_id, user_id=user.id)
    if response is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    return response


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
