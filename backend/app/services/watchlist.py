import math

from app.exceptions import DuplicateError, NotFoundError
from app.repositories.watchlist import WatchlistRepository
from app.schemas.news import PaginatedNewsResponse
from app.services.articles import build_brief


class WatchlistService:
    def __init__(self, repo: WatchlistRepository) -> None:
        self.repo = repo

    async def list_watchlist(
        self,
        user_id: int,
        page: int,
        per_page: int,
    ) -> PaginatedNewsResponse:
        articles, total = await self.repo.fetch_watched_articles(
            user_id, page, per_page
        )
        # All items are in the user's watchlist — build watched_ids from result
        watched_ids = {a.id for a in articles}

        return PaginatedNewsResponse(
            items=[build_brief(a, watched_ids) for a in articles],
            total=total,
            page=page,
            per_page=per_page,
            total_pages=math.ceil(total / per_page) if total > 0 else 0,
        )

    async def add_to_watchlist(self, user_id: int, news_id: int) -> None:
        if not await self.repo.article_exists(news_id):
            raise NotFoundError("News article not found")

        if await self.repo.find_entry(user_id, news_id):
            raise DuplicateError("Article already in watchlist")

        await self.repo.add_entry(user_id, news_id)

    async def remove_from_watchlist(self, user_id: int, news_id: int) -> None:
        entry = await self.repo.find_entry(user_id, news_id)
        if entry is None:
            raise NotFoundError("Watchlist item not found")

        await self.repo.remove_entry(entry)
