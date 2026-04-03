"""Endpoints for the authenticated user (watchlist)."""

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import CurrentUser, get_current_user, get_session
from app.exceptions import DuplicateError, NotFoundError
from app.repositories.watchlist import WatchlistRepository
from app.schemas.articles import PaginatedArticleResponse
from app.schemas.watchlist import WatchlistCreate
from app.services.watchlist import WatchlistService

router = APIRouter(prefix="/api/v1/me", tags=["me"])


def _service(session: AsyncSession) -> WatchlistService:
    return WatchlistService(WatchlistRepository(session))


@router.get("/watchlist", response_model=PaginatedArticleResponse)
async def list_watchlist(
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=100, alias="perPage"),
    user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> PaginatedArticleResponse:
    return await _service(session).list_watchlist(user.id, page, per_page)


@router.post("/watchlist", status_code=status.HTTP_201_CREATED)
async def add_to_watchlist(
    body: WatchlistCreate,
    user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> None:
    try:
        await _service(session).add_to_watchlist(user.id, body.news_id)
    except NotFoundError as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=e.detail)
    except DuplicateError as e:
        raise HTTPException(status.HTTP_409_CONFLICT, detail=e.detail)


@router.delete(
    "/watchlist/{news_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def remove_from_watchlist(
    news_id: int,
    user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> None:
    try:
        await _service(session).remove_from_watchlist(user.id, news_id)
    except NotFoundError as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=e.detail)
