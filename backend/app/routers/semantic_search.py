"""Semantic search endpoint for embedding-based article exploration."""

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import CurrentUser, get_optional_user, get_session
from app.repositories.semantic_search import SemanticSearchRepository
from app.repositories.watchlist import WatchlistRepository
from app.schemas.articles import PaginatedArticleResponse, SemanticSearchParams
from app.services.semantic_search import SemanticSearchService

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
    """Search articles by semantic similarity to the given query text."""
    return await service.search(params, user.id if user else None)
