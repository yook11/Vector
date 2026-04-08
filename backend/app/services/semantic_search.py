"""Semantic search service — embedding-based analytical exploration."""

from app.repositories.semantic_search import SemanticSearchRepository
from app.repositories.watchlist import WatchlistRepository
from app.schemas.articles import PaginatedArticleResponse, SemanticSearchParams
from app.services.articles import build_brief
from app.services.embedding import embed_search_query


class SemanticSearchService:
    def __init__(
        self,
        search_repo: SemanticSearchRepository,
        watchlist_repo: WatchlistRepository,
    ) -> None:
        self.search_repo = search_repo
        self.watchlist_repo = watchlist_repo

    async def search(
        self,
        query: SemanticSearchParams,
        user_id: int | None,
    ) -> PaginatedArticleResponse:
        """Search articles by semantic similarity to the user's query text."""
        query_embedding = await embed_search_query(query.q)
        analyses, total = await self.search_repo.search_articles(query, query_embedding)
        watched_ids = (
            await self.watchlist_repo.get_watched_ids(user_id) if user_id else set()
        )
        return PaginatedArticleResponse.create(
            items=[build_brief(a, watched_ids) for a in analyses],
            total=total,
            pagination=query,
        )
