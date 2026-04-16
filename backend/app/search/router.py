"""Embedding ベースの記事探索向けセマンティック検索エンドポイント。"""

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import CurrentUser, get_optional_user, get_session
from app.repositories.watchlist import WatchlistRepository
from app.schemas.articles import PaginatedArticleResponse, SemanticSearchParams
from app.search.repository import SemanticSearchRepository
from app.search.service import SemanticSearchService

router = APIRouter(prefix="/api/v1/articles", tags=["semantic-search"])


def get_semantic_search_service(
    session: AsyncSession = Depends(get_session),
) -> SemanticSearchService:
    return SemanticSearchService(
        search_repo=SemanticSearchRepository(session),
        watchlist_repo=WatchlistRepository(session),
    )


@router.get("/search", response_model=PaginatedArticleResponse)
async def search_articles(
    params: Annotated[SemanticSearchParams, Query()],
    user: CurrentUser | None = Depends(get_optional_user),
    service: SemanticSearchService = Depends(get_semantic_search_service),
) -> PaginatedArticleResponse:
    """指定クエリテキストとのセマンティック類似度で記事を検索する。"""
    return await service.search(params, user.id if user else None)
