"""Read endpoints for analyzed articles."""

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import CurrentUser, get_optional_user, get_session
from app.domain.category import CategorySlug
from app.exceptions import NotFoundError
from app.models.article_analysis import ImpactLevel
from app.repositories.articles import ArticleListParams, ArticleRepository
from app.schemas.articles import (
    ArticleBrief,
    ArticleDetail,
    PaginatedArticleResponse,
)
from app.services.articles import ArticleService
from app.services.embedding import EmbeddingError

router = APIRouter(prefix="/api/v1/articles", tags=["articles"])


@router.get("", response_model=PaginatedArticleResponse)
async def list_articles(
    keyword_id: int | None = Query(None, alias="keywordId"),
    category: str | None = Query(None),
    source: str | None = Query(None),
    impact_level: ImpactLevel | None = Query(None, alias="impactLevel"),
    q: str | None = Query(None, min_length=1, max_length=500),
    sort_by: str = Query("publishedAt", alias="sortBy"),
    sort_order: str = Query("desc", alias="sortOrder"),
    page: int = Query(1, ge=1),
    per_page: int = Query(12, ge=1, le=100, alias="perPage"),
    user: CurrentUser | None = Depends(get_optional_user),
    session: AsyncSession = Depends(get_session),
) -> PaginatedArticleResponse:
    """List analyzed articles with filters and pagination."""
    try:
        category_slug = CategorySlug(category) if category else None
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Invalid category slug: {category!r}",
        )
    service = ArticleService(ArticleRepository(session))
    params = ArticleListParams(
        keyword_id=keyword_id,
        category_slug=category_slug,
        source_name=source,
        impact_level=impact_level,
        sort_by=sort_by,
        sort_order=sort_order,
        page=page,
        per_page=per_page,
    )
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
    session: AsyncSession = Depends(get_session),
) -> list[ArticleBrief]:
    """Return articles most similar to the given article."""
    service = ArticleService(ArticleRepository(session))
    try:
        return await service.get_similar(article_id, limit)
    except NotFoundError as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=e.detail)


@router.get("/{article_id}", response_model=ArticleDetail)
async def get_article(
    article_id: int,
    user: CurrentUser | None = Depends(get_optional_user),
    session: AsyncSession = Depends(get_session),
) -> ArticleDetail:
    """Get a single article with full analysis details."""
    service = ArticleService(ArticleRepository(session))
    try:
        return await service.get_article(article_id, user.id if user else None)
    except NotFoundError as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=e.detail)
