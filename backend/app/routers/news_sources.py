"""CRUD endpoints for news_sources management."""

from fastapi import APIRouter, Depends, HTTPException, status
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


async def _get_or_404(repo: NewsSourceRepository, source_id: int):
    source = await repo.get_by_id(source_id)
    if source is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="News source not found",
        )
    return source


@router.get("", response_model=NewsSourceDetailList)
async def list_sources(
    session: AsyncSession = Depends(get_session),
    _user: CurrentUser = Depends(get_current_user),
) -> NewsSourceDetailList:
    """List all news sources."""
    repo = NewsSourceRepository(session)
    service = NewsSourceService(repo)
    return await service.list_sources()


@router.get("/{source_id}", response_model=NewsSourceDetail)
async def get_source(
    source_id: int,
    session: AsyncSession = Depends(get_session),
    _user: CurrentUser = Depends(get_current_user),
) -> NewsSourceDetail:
    """Get a single news source by ID."""
    repo = NewsSourceRepository(session)
    service = NewsSourceService(repo)
    source = await _get_or_404(repo, source_id)
    return await service.get_source(source)


@router.post(
    "",
    response_model=NewsSourceDetail,
    status_code=status.HTTP_201_CREATED,
)
async def create_source(
    body: NewsSourceCreate,
    session: AsyncSession = Depends(get_session),
    _user: CurrentUser = Depends(get_admin_user),
) -> NewsSourceDetail:
    """Create a new news source."""
    repo = NewsSourceRepository(session)
    service = NewsSourceService(repo)
    return await service.create_source(body)


@router.delete(
    "/{source_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_source(
    source_id: int,
    session: AsyncSession = Depends(get_session),
    _user: CurrentUser = Depends(get_admin_user),
) -> None:
    """Delete a news source."""
    repo = NewsSourceRepository(session)
    service = NewsSourceService(repo)
    source = await _get_or_404(repo, source_id)
    await service.delete_source(source)


@router.patch(
    "/{source_id}/toggle",
    response_model=NewsSourceDetail,
)
async def toggle_source(
    source_id: int,
    session: AsyncSession = Depends(get_session),
    _user: CurrentUser = Depends(get_admin_user),
) -> NewsSourceDetail:
    """Toggle a news source's is_active status."""
    repo = NewsSourceRepository(session)
    service = NewsSourceService(repo)
    source = await _get_or_404(repo, source_id)
    return await service.toggle_source(source)
