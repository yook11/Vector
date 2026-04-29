"""Embedding ベースの記事探索向けセマンティック検索エンドポイント。"""

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import get_session
from app.schemas.articles import PaginatedArticleResponse, SemanticSearchParams
from app.search.repository import SemanticSearchRepository
from app.search.service import SemanticSearchService

router = APIRouter(prefix="/api/v1/articles", tags=["semantic-search"])


def get_semantic_search_service(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> SemanticSearchService:
    return SemanticSearchService(search_repo=SemanticSearchRepository(session))


@router.get("/search")
async def search_articles(
    params: Annotated[SemanticSearchParams, Query()],
    service: Annotated[SemanticSearchService, Depends(get_semantic_search_service)],
) -> PaginatedArticleResponse:
    """指定クエリテキストとのセマンティック類似度で記事を検索する。

    レスポンスは user 非依存。per-user の watchlist 状態は
    GET /api/v1/me/watchlist/ids で別取得し frontend で merge する。
    """
    return await service.search(params)
