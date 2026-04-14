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

router = APIRouter(prefix="/pipeline", tags=["admin:pipeline"])


@router.post(
    "/fetch",
    response_model=FetchResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def fetch_news(
    body: FetchRequest | None = None,
) -> FetchResponse:
    """Enqueue a news fetch task. Returns immediately with a task ID."""
    from app.tasks.metadata_tasks import fetch_metadata

    source_ids = body.source_ids if body else None
    task = await fetch_metadata.kiq(source_ids=source_ids)
    return FetchResponse(
        message="Fetch task submitted",
        sources_count=len(source_ids) if source_ids else None,
        job_id=task.task_id,
    )


@router.post(
    "/embed",
    response_model=EmbedResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Dispatch embedding tasks for analyses that are missing them",
)
async def embed_news(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> EmbedResponse:
    """Enqueue generate_embedding tasks for all articles missing embeddings."""
    from app.tasks.embedding_tasks import generate_embedding

    repo = PipelineRepository(session)
    article_ids = await repo.get_article_ids_without_embedding()
    for article_id in article_ids:
        await generate_embedding.kiq(article_id)
    return EmbedResponse(
        message="Embedding tasks dispatched"
        if article_ids
        else "No articles need embedding",
        dispatched_count=len(article_ids),
    )
