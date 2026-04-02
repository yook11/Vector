"""CRUD endpoints for keyword management."""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import CurrentUser, get_admin_user, get_current_user, get_session
from app.exceptions import DuplicateError, NotFoundError, ReferenceNotFoundError
from app.repositories.keyword import KeywordRepository
from app.schemas.keyword import (
    KeywordCreate,
    KeywordDetail,
    KeywordDetailList,
    KeywordUpdate,
)
from app.services.keyword import KeywordService

router = APIRouter(prefix="/api/v1/keywords", tags=["keywords"])


@router.get("", response_model=KeywordDetailList)
async def list_keywords(
    _user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> KeywordDetailList:
    """List all keywords with category and article count."""
    service = KeywordService(KeywordRepository(session))
    return await service.list_keywords()


@router.post(
    "",
    response_model=KeywordDetail,
    status_code=status.HTTP_201_CREATED,
)
async def create_keyword(
    body: KeywordCreate,
    _user: CurrentUser = Depends(get_admin_user),
    session: AsyncSession = Depends(get_session),
) -> KeywordDetail:
    """Create a new keyword."""
    service = KeywordService(KeywordRepository(session))
    try:
        return await service.create_keyword(body)
    except DuplicateError as e:
        raise HTTPException(status.HTTP_409_CONFLICT, detail=e.detail)
    except ReferenceNotFoundError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=e.detail)


@router.patch("/{keyword_id}", response_model=KeywordDetail)
async def update_keyword(
    keyword_id: int,
    body: KeywordUpdate,
    _user: CurrentUser = Depends(get_admin_user),
    session: AsyncSession = Depends(get_session),
) -> KeywordDetail:
    """Update a keyword's category."""
    service = KeywordService(KeywordRepository(session))
    try:
        return await service.update_keyword(keyword_id, body)
    except NotFoundError as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=e.detail)
    except ReferenceNotFoundError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=e.detail)


@router.delete("/{keyword_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_keyword(
    keyword_id: int,
    _user: CurrentUser = Depends(get_admin_user),
    session: AsyncSession = Depends(get_session),
) -> None:
    """Delete a keyword."""
    service = KeywordService(KeywordRepository(session))
    try:
        await service.delete_keyword(keyword_id)
    except NotFoundError as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=e.detail)
