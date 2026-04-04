"""CRUD endpoints for news_sources management."""

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import CurrentUser, get_admin_user, get_current_user, get_session
from app.repositories.news_source import NewsSourceRepository
from app.schemas.news_source import (
    NewsSourceCreate,
    NewsSourceDetail,
    NewsSourceDetailList,
)
from app.services.news_source import NewsSourceService

router = APIRouter(prefix="/api/v1/sources", tags=["sources"])


def get_news_source_service(
    session: AsyncSession = Depends(get_session),
) -> NewsSourceService:
    return NewsSourceService(NewsSourceRepository(session))


@router.get("", response_model=NewsSourceDetailList)
async def list_sources(
    _user: CurrentUser = Depends(get_current_user),
    service: NewsSourceService = Depends(get_news_source_service),
) -> NewsSourceDetailList:
    """List all news sources."""
    return await service.list_sources()


@router.post(
    "",
    response_model=NewsSourceDetail,
    status_code=status.HTTP_201_CREATED,
)
async def create_source(
    body: NewsSourceCreate,
    _user: CurrentUser = Depends(get_admin_user),
    service: NewsSourceService = Depends(get_news_source_service),
) -> NewsSourceDetail:
    """Create a new news source."""
    return await service.create_source(body)


@router.delete(
    "/{source_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_source(
    source_id: int,
    _user: CurrentUser = Depends(get_admin_user),
    service: NewsSourceService = Depends(get_news_source_service),
) -> None:
    """Delete a news source."""
    await service.delete_source(source_id)


@router.patch(
    "/{source_id}/toggle",
    response_model=NewsSourceDetail,
)
async def toggle_source(
    source_id: int,
    _user: CurrentUser = Depends(get_admin_user),
    service: NewsSourceService = Depends(get_news_source_service),
) -> NewsSourceDetail:
    """Toggle a news source's is_active status."""
    return await service.toggle_source(source_id)
