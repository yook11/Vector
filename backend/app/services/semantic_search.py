"""Semantic search service — embedding-based analytical exploration."""

import math

from app.repositories.articles import ArticleRepository
from app.repositories.semantic_search import SemanticSearchRepository
from app.schemas.articles import PaginatedArticleResponse, SemanticSearchParams
from app.services.articles import build_brief
from app.services.embedding import embed_search_query


class SemanticSearchService:
    def __init__(
        self,
        search_repo: SemanticSearchRepository,
        article_repo: ArticleRepository,
    ) -> None:
        self.search_repo = search_repo
        self.article_repo = article_repo

    async def search(
        self,
        query: SemanticSearchParams,
        user_id: int | None,
    ) -> PaginatedArticleResponse:
        """Search articles by semantic similarity to the user's query text."""
        query_embedding = await embed_search_query(query.q)
        articles, total = await self.search_repo.search_articles(query, query_embedding)
        watched_ids = (
            await self.article_repo.get_watched_ids(user_id) if user_id else set()
        )
        return PaginatedArticleResponse(
            items=[build_brief(a, watched_ids) for a in articles],
            total=total,
            page=query.page,
            per_page=query.per_page,
            total_pages=math.ceil(total / query.per_page) if total > 0 else 0,
        )
