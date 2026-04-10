"""CRUD endpoints for news_sources management."""

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import get_session
from app.repositories.news_source import NewsSourceRepository
from app.schemas.news_source import (
    NewsSourceCreate,
    NewsSourceDetail,
    NewsSourceDetailList,
)
from app.services.news_source import NewsSourceService

router = APIRouter(prefix="/sources", tags=["admin:sources"])


def get_news_source_service(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> NewsSourceService:
    return NewsSourceService(NewsSourceRepository(session))


@router.get("", response_model=NewsSourceDetailList)
async def list_sources(
    service: Annotated[NewsSourceService, Depends(get_news_source_service)],
) -> NewsSourceDetailList:
    """List all news sources."""
    return await service.get_all()


@router.post(
    "",
    response_model=NewsSourceDetail,
    status_code=201,
)
async def create_source(
    body: NewsSourceCreate,
    service: Annotated[NewsSourceService, Depends(get_news_source_service)],
) -> NewsSourceDetail:
    """Create a new news source."""
    return await service.create_source(body)


@router.delete(
    "/{source_id}",
    status_code=204,
)
async def delete_source(
    source_id: int,
    service: Annotated[NewsSourceService, Depends(get_news_source_service)],
) -> None:
    """Delete a news source."""
    await service.delete_source(source_id)


@router.patch(
    "/{source_id}/toggle",
    response_model=NewsSourceDetail,
)
async def toggle_source(
    source_id: int,
    service: Annotated[NewsSourceService, Depends(get_news_source_service)],
) -> NewsSourceDetail:
    """Toggle a news source's is_active status."""
    return await service.toggle_source(source_id)
