"""Read endpoints for analyzed articles."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import CurrentUser, get_optional_user, get_session
from app.domain.category import CategorySlug
from app.domain.news_source import SourceName
from app.repositories.articles import ArticleRepository
from app.schemas.articles import (
    ArticleBrief,
    ArticleDetail,
    ArticleListParams,
    ArticleListQuery,
    PaginatedArticleResponse,
)
from app.services.articles import ArticleService

router = APIRouter(prefix="/api/v1/articles", tags=["articles"])


def get_article_service(
    session: AsyncSession = Depends(get_session),
) -> ArticleService:
    return ArticleService(ArticleRepository(session))


def _resolve_params(params: ArticleListParams) -> ArticleListQuery:
    """Convert raw request params to resolved query with VOs."""
    return ArticleListQuery(
        keyword_id=params.keyword_id,
        category_slug=CategorySlug(params.category) if params.category else None,
        source_name=SourceName(params.source) if params.source else None,
        impact_level=params.impact_level,
        q=params.q,
        sort_by=params.sort_by,
        sort_order=params.sort_order,
        page=params.page,
        per_page=params.per_page,
    )


@router.get("", response_model=PaginatedArticleResponse)
async def list_articles(
    params: Annotated[ArticleListParams, Query()],
    user: CurrentUser | None = Depends(get_optional_user),
    service: ArticleService = Depends(get_article_service),
) -> PaginatedArticleResponse:
    """List analyzed articles with filters and pagination."""
    try:
        query = _resolve_params(params)
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(e),
        )
    return await service.list_articles(query, user.id if user else None)


@router.get(
    "/{article_id}/similar",
    response_model=list[ArticleBrief],
    summary="Find semantically similar articles using pgvector cosine distance",
)
async def get_similar_articles(
    article_id: int,
    limit: int = Query(5, ge=1, le=20),
    service: ArticleService = Depends(get_article_service),
) -> list[ArticleBrief]:
    """Return articles most similar to the given article."""
    return await service.get_similar(article_id, limit)


@router.get("/{article_id}", response_model=ArticleDetail)
async def get_article(
    article_id: int,
    user: CurrentUser | None = Depends(get_optional_user),
    service: ArticleService = Depends(get_article_service),
) -> ArticleDetail:
    """Get a single article with full analysis details."""
    return await service.get_article(article_id, user.id if user else None)
