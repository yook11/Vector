"""認証ユーザー向けエンドポイント（ウォッチリスト）。"""

from typing import Annotated

from fastapi import APIRouter, Depends, Path, Query, Response, status

from app.db.fastapi import EntryManagedSession
from app.dependencies import CurrentUser, get_current_user
from app.repositories.articles import ArticleRepository
from app.repositories.watchlist import WatchlistRepository
from app.schemas.articles import PaginatedArticleResponse
from app.schemas.base import PaginationParams
from app.schemas.watchlist import WatchlistIds
from app.services.watchlist import WatchlistService

# article_id は PostgreSQL INTEGER (int32)。OverflowError 由来の 500 leak を
# 構造的に閉塞するため上限を path level で明示する (router/articles.py の
# _ArticleId と同型)。
_INT32_MAX = 2_147_483_647
_ArticleId = Annotated[int, Path(ge=1, le=_INT32_MAX)]

router = APIRouter(prefix="/api/v1/me", tags=["watchlist"])


def get_watchlist_service(
    user: Annotated[CurrentUser, Depends(get_current_user)],
    session: EntryManagedSession,
) -> WatchlistService:
    """ログイン中のユーザーに紐づいた WatchlistService を用意する。未ログインは 401。"""
    return WatchlistService(
        user.id,
        WatchlistRepository(session),
        ArticleRepository(session),
    )


@router.get("/watchlist/ids")
async def list_watchlist_ids(
    service: Annotated[WatchlistService, Depends(get_watchlist_service)],
) -> WatchlistIds:
    """ウォッチ中の article_id 集合を返す (per-user, cache 不可)。"""
    ids = await service.list_ids()
    return WatchlistIds(ids=ids)


@router.get("/watchlist")
async def list_articles_in_watchlist(
    pagination: Annotated[PaginationParams, Query()],
    service: Annotated[WatchlistService, Depends(get_watchlist_service)],
) -> PaginatedArticleResponse:
    return await service.list_articles_in_watchlist(pagination)


@router.put(
    "/watchlist/{article_id}",
    status_code=status.HTTP_201_CREATED,
    response_class=Response,
    response_description="Added to the watchlist",
    responses={
        status.HTTP_204_NO_CONTENT: {
            "description": "Article is already in the watchlist"
        },
        status.HTTP_404_NOT_FOUND: {"description": "News article not found"},
    },
)
async def add_to_watchlist(
    article_id: _ArticleId,
    service: Annotated[WatchlistService, Depends(get_watchlist_service)],
) -> Response:
    """記事をウォッチリストに入れる。新しく追加したら 201、登録済みなら 204 を返す。"""
    added = await service.add_to_watchlist(article_id)
    return Response(
        status_code=status.HTTP_201_CREATED if added else status.HTTP_204_NO_CONTENT
    )


@router.delete(
    "/watchlist/{article_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def remove_from_watchlist(
    article_id: _ArticleId,
    service: Annotated[WatchlistService, Depends(get_watchlist_service)],
) -> None:
    """記事をウォッチリストから外す。登録されていなくても 204 を返す。"""
    await service.remove_from_watchlist(article_id)
