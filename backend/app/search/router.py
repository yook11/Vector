"""Embedding ベースの記事探索向けセマンティック検索エンドポイント。"""

from typing import Annotated

import redis.asyncio as aioredis
from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.dependencies import (
    CurrentUser,
    get_current_user,
    get_redis_client,
    get_session,
)
from app.schemas.articles import PaginatedArticleResponse, SemanticSearchParams
from app.search.embedding.base import QueryEmbedder
from app.search.embedding.gemini import GeminiQueryEmbedder
from app.search.repository import SemanticSearchRepository
from app.search.service import SemanticSearchService

router = APIRouter(prefix="/api/v1/articles", tags=["semantic-search"])


def get_embedder_for_search() -> QueryEmbedder:
    """検索クエリ embedding 用の Pure DI composition root。

    本番経路では ``GeminiQueryEmbedder`` を hardcode する (env による provider
    切替なし)。CI / Schemathesis 等で外部 API を避けたい場合は
    ``app.dependency_overrides[get_embedder_for_search] = lambda: StubQueryEmbedder()``
    で差し替える。
    """
    return GeminiQueryEmbedder()


def get_semantic_search_service(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> SemanticSearchService:
    return SemanticSearchService(search_repo=SemanticSearchRepository(session))


@router.get("/search")
async def search_articles(
    params: Annotated[SemanticSearchParams, Query()],
    user: Annotated[CurrentUser, Depends(get_current_user)],
    redis: Annotated[aioredis.Redis, Depends(get_redis_client)],
    service: Annotated[SemanticSearchService, Depends(get_semantic_search_service)],
    embedder: Annotated[QueryEmbedder, Depends(get_embedder_for_search)],
) -> PaginatedArticleResponse:
    """セマンティック類似度で記事を検索する (red-team C1 対策: auth + per-user quota)。

    認証済みユーザーごとに 1 日 ``semantic_search_daily_quota_per_user`` 回まで
    embedding を生成する (Redis atomic counter)。cache hit はクォータを消費しない。
    anon access は 401、quota 超過は 429。

    レスポンスは user 非依存。per-user の watchlist 状態は
    GET /api/v1/me/watchlist/ids で別取得し frontend で merge する。
    """
    return await service.search(
        params,
        user_id=user.id,
        redis=redis,
        daily_max=settings.semantic_search_daily_quota_per_user,
        embedder=embedder,
    )
