"""Admin endpoints for pipeline operations (fetch, embed)."""

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import CurrentUser, get_admin_user, get_session
from app.repositories.pipeline import PipelineRepository
from app.schemas.pipeline import (
    EmbedResponse,
    FetchRequest,
    FetchResponse,
)
from app.services.pipeline import PipelineService

router = APIRouter(prefix="/api/v1/pipeline", tags=["pipeline"])


def get_pipeline_service(
    session: AsyncSession = Depends(get_session),
) -> PipelineService:
    return PipelineService(PipelineRepository(session))


@router.post(
    "/fetch",
    response_model=FetchResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def fetch_news(
    body: FetchRequest | None = None,
    _user: CurrentUser = Depends(get_admin_user),
) -> FetchResponse:
    """Enqueue a news fetch task. Returns immediately with a task ID."""
    source_ids = body.source_ids if body else None
    return await PipelineService.submit_fetch(source_ids)


@router.post(
    "/embed",
    response_model=EmbedResponse,
    status_code=status.HTTP_200_OK,
    summary="Backfill embeddings for analyses that are missing them",
)
async def embed_news(
    _user: CurrentUser = Depends(get_admin_user),
    service: PipelineService = Depends(get_pipeline_service),
) -> EmbedResponse:
    """Generate vector embeddings for all analyses where embedding IS NULL."""
    return await service.backfill_embeddings()
