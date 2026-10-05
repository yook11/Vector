"""認証ユーザー向けエンドポイント（ウォッチリスト）。"""

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response, status

from app.db.fastapi import EntryManagedSession
from app.dependencies import AuthenticatedUser, require_authenticated_user
from app.repositories.articles import ArticleRepository
from app.repositories.watchlist import WatchlistRepository
from app.schemas.articles import AnalyzedArticlePreviewList, ArticleId
from app.schemas.watchlist import WatchlistIds, WatchlistIdsParams, WatchlistParams
from app.services.watchlist import WatchlistService

router = APIRouter(
    prefix="/api/v1/me",
    tags=["watchlist"],
    dependencies=[Depends(require_authenticated_user)],
)


def get_watchlist_service(
    user: Annotated[AuthenticatedUser, Depends(require_authenticated_user)],
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
    params: Annotated[WatchlistIdsParams, Query()],
    service: Annotated[WatchlistService, Depends(get_watchlist_service)],
) -> WatchlistIds:
    """渡した記事のうち、ウォッチ中のものの ID を返す。"""
    ids = await service.list_watched_ids(params.article_ids)
    return WatchlistIds(ids=ids)


@router.get("/watchlist")
async def list_articles_in_watchlist(
    params: Annotated[WatchlistParams, Query()],
    service: Annotated[WatchlistService, Depends(get_watchlist_service)],
) -> AnalyzedArticlePreviewList:
    """ウォッチ中の記事を新しく入れた順に1回分取得する。続きは nextCursor で取る。"""
    return await service.list_articles_in_watchlist(params)


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
    article_id: ArticleId,
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
    article_id: ArticleId,
    service: Annotated[WatchlistService, Depends(get_watchlist_service)],
) -> None:
    """記事をウォッチリストから外す。登録されていなくても 204 を返す。"""
    await service.remove_from_watchlist(article_id)
