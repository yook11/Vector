"""Admin endpoints for pipeline operations (fetch, embed)."""

from typing import Annotated

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import get_session
from app.repositories.pipeline import PipelineRepository
from app.schemas.pipeline import (
    EmbedResponse,
    FetchRequest,
    FetchResponse,
)
from app.services.pipeline import PipelineService

router = APIRouter(prefix="/pipeline", tags=["admin:pipeline"])


def get_pipeline_service(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> PipelineService:
    return PipelineService(PipelineRepository(session))


@router.post(
    "/fetch",
    response_model=FetchResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def fetch_news(
    body: FetchRequest | None = None,
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
    service: Annotated[PipelineService, Depends(get_pipeline_service)],
) -> EmbedResponse:
    """Generate vector embeddings for all analyses where embedding IS NULL."""
    return await service.backfill_embeddings()
