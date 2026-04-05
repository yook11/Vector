"""Endpoints for the authenticated user (watchlist)."""

from typing import Annotated

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import CurrentUser, get_current_user, get_session
from app.repositories.watchlist import WatchlistRepository
from app.schemas.articles import PaginatedArticleResponse
from app.schemas.base import PaginationParams
from app.schemas.watchlist import WatchlistCreate
from app.services.watchlist import WatchlistService

router = APIRouter(prefix="/api/v1/me", tags=["watchlist"])


def get_watchlist_service(
    session: AsyncSession = Depends(get_session),
) -> WatchlistService:
    return WatchlistService(WatchlistRepository(session))


@router.get("/watchlist", response_model=PaginatedArticleResponse)
async def list_watchlist(
    pagination: Annotated[PaginationParams, Query()],
    user: CurrentUser = Depends(get_current_user),
    service: WatchlistService = Depends(get_watchlist_service),
) -> PaginatedArticleResponse:
    return await service.list_watchlist(user.id, pagination.page, pagination.per_page)


@router.post("/watchlist", status_code=status.HTTP_201_CREATED)
async def add_to_watchlist(
    body: WatchlistCreate,
    user: CurrentUser = Depends(get_current_user),
    service: WatchlistService = Depends(get_watchlist_service),
) -> None:
    await service.add_to_watchlist(user.id, body.news_id)


@router.delete(
    "/watchlist/{news_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def remove_from_watchlist(
    news_id: int,
    user: CurrentUser = Depends(get_current_user),
    service: WatchlistService = Depends(get_watchlist_service),
) -> None:
    await service.remove_from_watchlist(user.id, news_id)
