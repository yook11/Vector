"""CRUD endpoints for news articles."""

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import CurrentUser, get_admin_user, get_optional_user, get_session
from app.exceptions import NotFoundError
from app.models.article_analysis import ImpactLevel
from app.repositories.news import NewsListParams, NewsRepository
from app.schemas.news import (
    EmbedResponse,
    NewsBrief,
    NewsDetail,
    NewsFetchRequest,
    NewsFetchResponse,
    PaginatedNewsResponse,
)
from app.services.embedding import EmbeddingError
from app.services.news import NewsService

router = APIRouter(prefix="/api/v1/news", tags=["news"])


@router.get("", response_model=PaginatedNewsResponse)
async def list_news(
    keyword_id: int | None = Query(None, alias="keywordId"),
    kw_category_id: int | None = Query(None, alias="kwCategoryId"),
    source_id: int | None = Query(None, alias="sourceId"),
    impact_level: ImpactLevel | None = Query(None, alias="impactLevel"),
    q: str | None = Query(None, min_length=1, max_length=500),
    sort_by: str = Query("publishedAt", alias="sortBy"),
    sort_order: str = Query("desc", alias="sortOrder"),
    page: int = Query(1, ge=1),
    per_page: int = Query(12, ge=1, le=100, alias="perPage"),
    user: CurrentUser | None = Depends(get_optional_user),
    session: AsyncSession = Depends(get_session),
) -> PaginatedNewsResponse:
    """List analyzed news articles with filters and pagination."""
    service = NewsService(NewsRepository(session))
    params = NewsListParams(
        keyword_id=keyword_id,
        kw_category_id=kw_category_id,
        source_id=source_id,
        impact_level=impact_level,
        sort_by=sort_by,
        sort_order=sort_order,
        page=page,
        per_page=per_page,
    )
    try:
        return await service.list_news(params, q, user.id if user else None)
    except EmbeddingError:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Search embedding generation failed. Please try again.",
        )


@router.post(
    "/embed",
    response_model=EmbedResponse,
    status_code=status.HTTP_200_OK,
    summary="Backfill embeddings for analyses that are missing them",
)
async def embed_news(
    session: AsyncSession = Depends(get_session),
    _user: CurrentUser = Depends(get_admin_user),
) -> EmbedResponse:
    """Generate vector embeddings for all analyses where embedding IS NULL."""
    service = NewsService(NewsRepository(session))
    return await service.embed_news()


@router.get(
    "/{news_id}/similar",
    response_model=list[NewsBrief],
    summary="Find semantically similar articles using pgvector cosine distance",
)
async def get_similar_news(
    news_id: int,
    limit: int = Query(5, ge=1, le=20),
    session: AsyncSession = Depends(get_session),
) -> list[NewsBrief]:
    """Return articles most similar to the given article."""
    service = NewsService(NewsRepository(session))
    try:
        return await service.get_similar(news_id, limit)
    except NotFoundError as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=e.detail)


@router.get("/{news_id}", response_model=NewsDetail)
async def get_news(
    news_id: int,
    user: CurrentUser | None = Depends(get_optional_user),
    session: AsyncSession = Depends(get_session),
) -> NewsDetail:
    """Get a single news article with full analysis details."""
    service = NewsService(NewsRepository(session))
    try:
        return await service.get_news(news_id, user.id if user else None)
    except NotFoundError as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=e.detail)


@router.post(
    "/fetch",
    response_model=NewsFetchResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def fetch_news(
    body: NewsFetchRequest | None = None,
    _user: CurrentUser = Depends(get_admin_user),
) -> NewsFetchResponse:
    """Enqueue a news fetch task. Returns immediately with a task ID."""
    source_ids = body.source_ids if body else None
    return await NewsService.fetch_news(source_ids)
