"""Embedding ベースの記事探索向けセマンティック検索エンドポイント。"""

from typing import Annotated

import structlog
from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import CurrentUser, get_optional_user, get_session
from app.repositories.watchlist import WatchlistRepository
from app.schemas.articles import PaginatedArticleResponse, SemanticSearchParams
from app.search.repository import SemanticSearchRepository
from app.search.service import SemanticSearchService

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/api/v1/articles", tags=["semantic-search"])


def get_semantic_search_service(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> SemanticSearchService:
    return SemanticSearchService(
        search_repo=SemanticSearchRepository(session),
        watchlist_repo=WatchlistRepository(session),
    )


@router.get("/search")
async def search_articles(
    params: Annotated[SemanticSearchParams, Query()],
    user: Annotated[CurrentUser | None, Depends(get_optional_user)],
    service: Annotated[SemanticSearchService, Depends(get_semantic_search_service)],
) -> PaginatedArticleResponse:
    """指定クエリテキストとのセマンティック類似度で記事を検索する。"""
    if params.category is not None:
        logger.info("legacy_category_query_received", category=str(params.category))
    return await service.search(params, user.id if user else None)
