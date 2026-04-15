"""Semantic search service — embedding-based analytical exploration."""

from app.ai.embedding.service import embed_search_query
from app.repositories.semantic_search import SemanticSearchRepository
from app.repositories.watchlist import WatchlistRepository
from app.schemas.articles import PaginatedArticleResponse, SemanticSearchParams
from app.services.articles import build_brief


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

        watched_ids: set[int] = set()
        if user_id and analyses:
            article_ids = {a.id for a in analyses}
            watched_ids = await self.watchlist_repo.watched_among(user_id, article_ids)

        return PaginatedArticleResponse.create(
            items=[build_brief(a, watched_ids) for a in analyses],
            total=total,
            pagination=query,
        )
