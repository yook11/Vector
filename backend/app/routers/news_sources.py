"""CRUD endpoints for news_sources management."""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import CurrentUser, get_admin_user, get_current_user, get_session
from app.exceptions import NotFoundError
from app.repositories.news_source import NewsSourceRepository
from app.schemas.news_source import (
    NewsSourceCreate,
    NewsSourceDetail,
    NewsSourceDetailList,
)
from app.services.news_source import NewsSourceService

router = APIRouter(prefix="/api/v1/sources", tags=["sources"])


def _service(session: AsyncSession) -> NewsSourceService:
    return NewsSourceService(NewsSourceRepository(session))


@router.get("", response_model=NewsSourceDetailList)
async def list_sources(
    session: AsyncSession = Depends(get_session),
    _user: CurrentUser = Depends(get_current_user),
) -> NewsSourceDetailList:
    """List all news sources."""
    return await _service(session).list_sources()


@router.get("/{source_id}", response_model=NewsSourceDetail)
async def get_source(
    source_id: int,
    session: AsyncSession = Depends(get_session),
    _user: CurrentUser = Depends(get_current_user),
) -> NewsSourceDetail:
    """Get a single news source by ID."""
    try:
        return await _service(session).get_source(source_id)
    except NotFoundError as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=e.detail)


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
    return await _service(session).create_source(body)


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
    try:
        await _service(session).delete_source(source_id)
    except NotFoundError as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=e.detail)


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
    try:
        return await _service(session).toggle_source(source_id)
    except NotFoundError as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=e.detail)
