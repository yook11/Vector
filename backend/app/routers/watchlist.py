"""Endpoints for the authenticated user (watchlist)."""

from typing import Annotated

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import CurrentUser, get_current_user, get_session
from app.repositories.articles import ArticleRepository
from app.repositories.watchlist import WatchlistRepository
from app.schemas.articles import PaginatedArticleResponse
from app.schemas.base import PaginationParams
from app.schemas.watchlist import WatchlistCreate
from app.services.watchlist import WatchlistService

router = APIRouter(prefix="/api/v1/me", tags=["watchlist"])


def get_watchlist_service(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> WatchlistService:
    return WatchlistService(WatchlistRepository(session), ArticleRepository(session))


@router.get("/watchlist")
async def list_watchlist(
    pagination: Annotated[PaginationParams, Query()],
    user: Annotated[CurrentUser, Depends(get_current_user)],
    service: Annotated[WatchlistService, Depends(get_watchlist_service)],
) -> PaginatedArticleResponse:
    return await service.list_watchlist(user.id, pagination)


@router.post("/watchlist", status_code=status.HTTP_201_CREATED)
async def add_to_watchlist(
    body: WatchlistCreate,
    user: Annotated[CurrentUser, Depends(get_current_user)],
    service: Annotated[WatchlistService, Depends(get_watchlist_service)],
) -> None:
    await service.add_to_watchlist(user.id, body.article_id)


@router.delete(
    "/watchlist/{article_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def remove_from_watchlist(
    article_id: int,
    user: Annotated[CurrentUser, Depends(get_current_user)],
    service: Annotated[WatchlistService, Depends(get_watchlist_service)],
) -> None:
    await service.remove_from_watchlist(user.id, article_id)
