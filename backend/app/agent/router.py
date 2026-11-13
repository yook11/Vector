"""Research async run API router."""

from __future__ import annotations

import asyncio
import math
import time
from datetime import datetime, timedelta
from typing import Annotated
from uuid import UUID

from fastapi import (
    APIRouter,
    BackgroundTasks,
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

from app.agent.live_updates.run_reading import (
    AgentRunLiveRepository,
    OwnedAgentRunLiveContext,
)
from app.agent.live_updates.sse import (
    AgentRunQueuedSseConnection,
    AgentRunSseCapacity,
    AgentRunSsePreflightFailure,
    AgentRunSseTiming,
    prepare_running_sse_connection,
    validate_redis_stream_id,
)
from app.agent.live_updates.sse_response import AgentRunSseStreamingResponse
from app.agent.live_updates.stream import AGENT_RUN_LIVE_STREAM_TIMEOUT_SECONDS
from app.agent.live_updates.transport import (
    AgentLiveTransport,
    get_agent_live_transport,
)
from app.agent.run_enqueuer import AgentRunEnqueuer, get_agent_run_enqueuer
from app.agent.running.daily_quota.policy import DAILY_QUOTA_TIMEZONE
from app.agent.running.deadline.scheduling import (
    AgentDeadlineScheduler,
    get_agent_deadline_scheduler,
)
from app.agent.running.state import (
    ActiveRunExists,
    CancelledByUser,
    CannotContinue,
    DailyLimitReached,
    RunCreated,
    RunLeftQueuedError,
    RunNotFound,
    ThreadNotFound,
    has_attempt_started,
    is_finished,
)
from app.agent.running.state_service import AgentRunStateService
from app.agent.threads.detail import read_owned_thread_detail
from app.agent.threads.repository import AgentThreadRepository
from app.db.fastapi import get_caller_managed_session
from app.dependencies import (
    AuthenticatedUser,
    require_authenticated_user,
)
from app.log_policy.policies.research import RESEARCH_LOG_RULES
from app.log_policy.runtime import create_policy_json_logger
from app.schemas.research import (
    PaginatedResearchThreadResponse,
    ResearchDailyRequestLimitExceededResponse,
    ResearchQuestionRequest,
    ResearchRunResponse,
    ResearchRunStartResponse,
    ResearchThreadDetail,
    ResearchThreadListParams,
)

router = APIRouter(
    prefix="/api/v1/research",
    tags=["research"],
    dependencies=[Depends(require_authenticated_user)],
)

logger = create_policy_json_logger(__name__, RESEARCH_LOG_RULES)

_ACTIVE_RUN_DETAIL = "A run is already in progress for this thread"
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
        return await AgentRunLiveRepository(session).read_live_context_for_user(
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


def get_agent_run_state_service(
    request: Request,
    live: Annotated[AgentLiveTransport, Depends(get_agent_live_transport)],
) -> AgentRunStateService:
    return AgentRunStateService(
        session_factory=request.app.state.session_factory,
        publisher_for=live.publisher,
    )


@router.post(
    "/responses",
    operation_id="create_research_response",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=ResearchRunStartResponse,
    responses={
        status.HTTP_503_SERVICE_UNAVAILABLE: {
            "description": "Failed to enqueue research run"
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
    background_tasks: BackgroundTasks,
    deadline_scheduler: Annotated[
        AgentDeadlineScheduler, Depends(get_agent_deadline_scheduler)
    ],
    user: Annotated[AuthenticatedUser, Depends(require_authenticated_user)],
    enqueuer: Annotated[AgentRunEnqueuer, Depends(get_agent_run_enqueuer)],
    state_service: Annotated[
        AgentRunStateService, Depends(get_agent_run_state_service)
    ],
) -> ResearchRunStartResponse | JSONResponse:
    def schedule_deadline_check(run_id: UUID, deadline_at: datetime) -> None:
        # 応答を待たせないよう、期限の確認の予約は応答を送った後に行う。
        background_tasks.add_task(deadline_scheduler.reserve, run_id, deadline_at)

    try:
        result = await state_service.create_run(
            user_id=user.id,
            question=body.question,
            thread_id=body.thread_id,
            enqueuer=enqueuer,
            schedule_deadline_check=schedule_deadline_check,
        )
    except RunLeftQueuedError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Failed to enqueue research run",
        ) from exc
    match result:
        case RunCreated(thread_id=thread_id, run_id=run_id):
            return ResearchRunStartResponse(thread_id=thread_id, run_id=run_id)
        case ThreadNotFound():
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
        case ActiveRunExists():
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=_ACTIVE_RUN_DETAIL,
            )
        case DailyLimitReached(
            limit=limit, usage_date=usage_date, decided_at=decided_at
        ):
            reset_at = datetime.combine(
                usage_date + timedelta(days=1),
                datetime.min.time(),
                tzinfo=DAILY_QUOTA_TIMEZONE,
            )
            retry_after = max(0, math.ceil((reset_at - decided_at).total_seconds()))
            response = ResearchDailyRequestLimitExceededResponse(
                detail="Daily research request limit exceeded",
                code="research_daily_request_limit_exceeded",
                limit=limit,
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


@router.get(
    "/threads",
    operation_id="list_research_threads",
    response_model=PaginatedResearchThreadResponse,
)
async def list_research_threads(
    pagination: Annotated[ResearchThreadListParams, Query()],
    user: Annotated[AuthenticatedUser, Depends(require_authenticated_user)],
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
    user: Annotated[AuthenticatedUser, Depends(require_authenticated_user)],
    session: Annotated[AsyncSession, Depends(get_caller_managed_session)],
    state_service: Annotated[
        AgentRunStateService, Depends(get_agent_run_state_service)
    ],
) -> ResearchThreadDetail:
    response = await read_owned_thread_detail(
        session,
        thread_id=thread_id,
        user_id=user.id,
        state_service=state_service,
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
    user: Annotated[AuthenticatedUser, Depends(require_authenticated_user)],
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
    },
)
async def cancel_research_run(
    run_id: UUID,
    user: Annotated[AuthenticatedUser, Depends(require_authenticated_user)],
    state_service: Annotated[
        AgentRunStateService, Depends(get_agent_run_state_service)
    ],
) -> Response:
    match await state_service.cancel_for_user(run_id, user_id=user.id):
        case RunNotFound():
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
        case CancelledByUser() | CannotContinue():
            return Response(status_code=status.HTTP_204_NO_CONTENT)


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
    user: Annotated[AuthenticatedUser, Depends(require_authenticated_user)],
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
        if is_finished(context.status):
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
        if not has_attempt_started(context.attempt_epoch):
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
    user: Annotated[AuthenticatedUser, Depends(require_authenticated_user)],
    session: Annotated[AsyncSession, Depends(get_caller_managed_session)],
) -> ResearchRunResponse:
    repo = AgentRunLiveRepository(session)
    response = await repo.read_run_for_user(run_id=run_id, user_id=user.id)
    if response is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    return response


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
