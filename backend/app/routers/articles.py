"""Read endpoints for analyzed articles."""

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import CurrentUser, get_optional_user, get_session
from app.exceptions import NotFoundError
from app.repositories.articles import ArticleListParams, ArticleRepository
from app.schemas.articles import (
    ArticleBrief,
    ArticleDetail,
    PaginatedArticleResponse,
)
from app.services.articles import ArticleService
from app.services.embedding import EmbeddingError

router = APIRouter(prefix="/api/v1/articles", tags=["articles"])


def get_article_service(
    session: AsyncSession = Depends(get_session),
) -> ArticleService:
    return ArticleService(ArticleRepository(session))


@router.get("", response_model=PaginatedArticleResponse)
async def list_articles(
    q: str | None = Query(None, min_length=1, max_length=500),
    user: CurrentUser | None = Depends(get_optional_user),
    service: ArticleService = Depends(get_article_service),
    params: ArticleListParams = Depends(),
) -> PaginatedArticleResponse:
    """List analyzed articles with filters and pagination."""
    try:
        return await service.list_articles(params, q, user.id if user else None)
    except EmbeddingError:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Search embedding generation failed. Please try again.",
        )


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
    try:
        return await service.get_similar(article_id, limit)
    except NotFoundError as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=e.detail)


@router.get("/{article_id}", response_model=ArticleDetail)
async def get_article(
    article_id: int,
    user: CurrentUser | None = Depends(get_optional_user),
    service: ArticleService = Depends(get_article_service),
) -> ArticleDetail:
    """Get a single article with full analysis details."""
    try:
        return await service.get_article(article_id, user.id if user else None)
    except NotFoundError as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=e.detail)
