"""認証ユーザー向けエンドポイント（ウォッチリスト）。"""

from typing import Annotated

from fastapi import APIRouter, Depends, Path, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import CurrentUser, get_current_user, get_session
from app.repositories.articles import ArticleRepository
from app.repositories.watchlist import WatchlistRepository
from app.schemas.articles import PaginatedArticleResponse
from app.schemas.base import PaginationParams
from app.schemas.watchlist import WatchlistCreate, WatchlistIds
from app.services.watchlist import WatchlistService

# article_id は PostgreSQL INTEGER (int32)。OverflowError 由来の 500 leak を
# 構造的に閉塞するため上限を path level で明示する (router/articles.py の
# _ArticleId と同型)。
_INT32_MAX = 2_147_483_647
_ArticleId = Annotated[int, Path(ge=1, le=_INT32_MAX)]

router = APIRouter(prefix="/api/v1/me", tags=["watchlist"])


def get_watchlist_service(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> WatchlistService:
    return WatchlistService(WatchlistRepository(session), ArticleRepository(session))


@router.get("/watchlist/ids")
async def list_watchlist_ids(
    user: Annotated[CurrentUser, Depends(get_current_user)],
    service: Annotated[WatchlistService, Depends(get_watchlist_service)],
) -> WatchlistIds:
    """ウォッチ中の article_id 集合を返す (per-user, cache 不可)。"""
    ids = await service.list_ids(user.id)
    return WatchlistIds(ids=ids)


@router.get("/watchlist")
async def list_articles_in_watchlist(
    pagination: Annotated[PaginationParams, Query()],
    user: Annotated[CurrentUser, Depends(get_current_user)],
    service: Annotated[WatchlistService, Depends(get_watchlist_service)],
) -> PaginatedArticleResponse:
    return await service.list_articles_in_watchlist(user.id, pagination)


@router.post(
    "/watchlist",
    status_code=status.HTTP_201_CREATED,
    responses={404: {"description": "News article not found"}},
)
async def add_to_watchlist(
    body: WatchlistCreate,
    user: Annotated[CurrentUser, Depends(get_current_user)],
    service: Annotated[WatchlistService, Depends(get_watchlist_service)],
) -> None:
    await service.add_to_watchlist(user.id, body.article_id)


@router.delete(
    "/watchlist/{article_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={404: {"description": "Watchlist item not found"}},
)
async def remove_from_watchlist(
    article_id: _ArticleId,
    user: Annotated[CurrentUser, Depends(get_current_user)],
    service: Annotated[WatchlistService, Depends(get_watchlist_service)],
) -> None:
    await service.remove_from_watchlist(user.id, article_id)
