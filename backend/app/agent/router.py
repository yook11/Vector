"""Research async run API router."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Annotated
from uuid import UUID

import redis.asyncio as aioredis
import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.composition import ensure_question_answering_agent_configured
from app.agent.live_updates.recent_events import AgentRunLiveEventReader
from app.agent.runs.contracts import (
    ActiveRunConflictError,
    CancelRunOutcome,
    ThreadNotFoundError,
)
from app.agent.runs.repository import AgentRunRepository
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


async def get_agent_persistence_session() -> AsyncGenerator[AsyncSession]:
    # get_session の request-wide UoW は commit→kiq→failed 更新の 2 tx 制御と分ける。
    async with AsyncSession(engine, expire_on_commit=False) as session:
        yield session


async def enqueue_agent_run(run_id: UUID) -> None:
    from app.queue.messages.agent_run import AgentRunTrigger
    from app.queue.tasks.agent_run import run_agent_answer

    await run_agent_answer.kiq(AgentRunTrigger(run_id=run_id))


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
) -> Response:
    repo = AgentRunRepository(session)
    async with session.begin():
        outcome = await repo.cancel_run_for_user(run_id=run_id, user_id=user.id)
    if outcome is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    if outcome is CancelRunOutcome.ALREADY_COMPLETED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=_RUN_ALREADY_COMPLETED_DETAIL,
        )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


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
