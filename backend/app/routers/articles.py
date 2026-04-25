"""分析済み記事の参照系エンドポイント。"""

from typing import Annotated

import structlog
from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import CurrentUser, get_optional_user, get_session
from app.repositories.articles import ArticleRepository
from app.repositories.watchlist import WatchlistRepository
from app.schemas.articles import (
    ArticleBrief,
    ArticleDetail,
    ArticleListParams,
    PaginatedArticleResponse,
)
from app.services.articles import ArticleService

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/api/v1/articles", tags=["articles"])


def get_article_service(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> ArticleService:
    return ArticleService(ArticleRepository(session), WatchlistRepository(session))


@router.get("")
async def list_articles(
    params: Annotated[ArticleListParams, Query()],
    user: Annotated[CurrentUser | None, Depends(get_optional_user)],
    service: Annotated[ArticleService, Depends(get_article_service)],
) -> PaginatedArticleResponse:
    """分析済み記事をフィルタとページネーション付きで一覧取得する。"""
    if params.category is not None:
        logger.info("legacy_category_query_received", category=str(params.category))
    return await service.list_articles(params, user.id if user else None)


@router.get(
    "/{article_id}/similar",
    summary="pgvector のコサイン距離で意味的に類似した記事を検索する",
)
async def get_similar_articles(
    article_id: int,
    service: Annotated[ArticleService, Depends(get_article_service)],
    limit: Annotated[int, Query(ge=1, le=20)] = 5,
) -> list[ArticleBrief]:
    """指定記事に最も類似した記事を返す。"""
    return await service.get_similar(article_id, limit)


@router.get("/{article_id}")
async def get_article(
    article_id: int,
    user: Annotated[CurrentUser | None, Depends(get_optional_user)],
    service: Annotated[ArticleService, Depends(get_article_service)],
) -> ArticleDetail:
    """単一記事を完全な分析情報付きで取得する。"""
    return await service.get_article(article_id, user.id if user else None)
