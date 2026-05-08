"""分析済み記事の参照系エンドポイント。"""

from typing import Annotated

from fastapi import APIRouter, Depends, Path, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import get_session
from app.repositories.articles import ArticleRepository
from app.schemas.articles import (
    ArticleBrief,
    ArticleDetail,
    ArticleListParams,
    PaginatedArticleResponse,
)
from app.services.articles import ArticleService

router = APIRouter(prefix="/api/v1/articles", tags=["articles"])

# `ArticleAnalysis.id` は SQLAlchemy Mapped[int] = INTEGER (PostgreSQL int4, 32bit)。
# 上限を明示しないと Schemathesis 等が int64 域の値を投げてきたとき asyncpg が
# OverflowError → 500 を leak する。OpenAPI に上限が露出することで Schemathesis 側も
# 範囲内の値しか生成しなくなる。
_INT32_MAX = 2_147_483_647
_ArticleId = Annotated[int, Path(ge=1, le=_INT32_MAX)]


def get_article_service(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> ArticleService:
    return ArticleService(ArticleRepository(session))


@router.get("")
async def list_articles(
    params: Annotated[ArticleListParams, Query()],
    service: Annotated[ArticleService, Depends(get_article_service)],
) -> PaginatedArticleResponse:
    """分析済み記事をフィルタとページネーション付きで一覧取得する。

    レスポンスは user 非依存。per-user の watchlist 状態は
    GET /api/v1/me/watchlist/ids で別取得し frontend で merge する。
    """
    return await service.list_articles(params)


@router.get(
    "/{article_id}/similar",
    summary="pgvector のコサイン距離で意味的に類似した記事を検索する",
    responses={404: {"description": "News article not found"}},
)
async def get_similar_articles(
    article_id: _ArticleId,
    service: Annotated[ArticleService, Depends(get_article_service)],
    limit: Annotated[int, Query(ge=1, le=20)] = 5,
) -> list[ArticleBrief]:
    """指定記事に最も類似した記事を返す。"""
    return await service.get_similar(article_id, limit)


@router.get(
    "/{article_id}",
    responses={404: {"description": "News article not found"}},
)
async def get_article(
    article_id: _ArticleId,
    service: Annotated[ArticleService, Depends(get_article_service)],
) -> ArticleDetail:
    """単一記事を完全な分析情報付きで取得する。"""
    return await service.get_article(article_id)
