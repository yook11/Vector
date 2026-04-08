from uuid import UUID

from app.exceptions import DuplicateError, NotFoundError
from app.repositories.watchlist import WatchlistRepository
from app.schemas.articles import PaginatedArticleResponse
from app.schemas.base import PaginationParams
from app.services.articles import build_brief


class WatchlistService:
    def __init__(self, repo: WatchlistRepository) -> None:
        self.repo = repo

    async def list_watchlist(
        self,
        user_id: UUID,
        pagination: PaginationParams,
    ) -> PaginatedArticleResponse:
        analyses, total = await self.repo.fetch_watched_articles(user_id, pagination)
        # All items are in the user's watchlist — build watched_ids from result
        watched_ids = {a.id for a in analyses}

        return PaginatedArticleResponse.create(
            items=[build_brief(a, watched_ids) for a in analyses],
            total=total,
            pagination=pagination,
        )

    async def add_to_watchlist(self, user_id: UUID, article_id: int) -> None:
        if not await self.repo.article_exists(article_id):
            raise NotFoundError("News article not found")

        if await self.repo.find_entry(user_id, article_id):
            raise DuplicateError("Article already in watchlist")

        await self.repo.add_entry(user_id, article_id)

    async def remove_from_watchlist(self, user_id: UUID, article_id: int) -> None:
        entry = await self.repo.find_entry(user_id, article_id)
        if entry is None:
            raise NotFoundError("Watchlist item not found")

        await self.repo.remove_entry(entry)
