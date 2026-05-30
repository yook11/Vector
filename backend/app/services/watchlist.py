from uuid import UUID

from app.exceptions import DuplicateError, NotFoundError
from app.repositories.articles import ArticleRepository
from app.repositories.watchlist import WatchlistRepository
from app.schemas.articles import PaginatedArticleResponse
from app.schemas.base import PaginationParams
from app.services.articles import build_brief


class WatchlistService:
    def __init__(
        self,
        repo: WatchlistRepository,
        article_repo: ArticleRepository,
    ) -> None:
        self.repo = repo
        self.article_repo = article_repo

    async def list_articles_in_watchlist(
        self,
        user_id: UUID,
        pagination: PaginationParams,
    ) -> PaginatedArticleResponse:
        analyses, total = await self.repo.fetch_watched_articles(user_id, pagination)
        return PaginatedArticleResponse.create(
            items=[build_brief(a) for a in analyses],
            total=total,
            pagination=pagination,
        )

    async def list_ids(self, user_id: UUID) -> list[int]:
        """ユーザーがウォッチ中の article_id を新しい順に返す。"""
        return await self.repo.list_ids(user_id)

    async def add_to_watchlist(self, user_id: UUID, article_id: int) -> None:
        if not await self.article_repo.exists_analyzed(article_id):
            raise NotFoundError("News article not found")

        if await self.repo.is_watched(user_id, article_id):
            # 公開 detail は exception_handlers の allowlist 形式に揃える。
            raise DuplicateError("Watchlist entry already exists")

        await self.repo.watch(user_id, article_id)

    async def remove_from_watchlist(self, user_id: UUID, article_id: int) -> None:
        if not await self.repo.is_watched(user_id, article_id):
            raise NotFoundError("Watchlist item not found")
        await self.repo.unwatch(user_id, article_id)
