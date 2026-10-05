from uuid import UUID

from app.exceptions import NotFoundError
from app.repositories.articles import ArticleRepository
from app.repositories.watchlist import WatchlistRepository
from app.schemas.articles import PaginatedArticleResponse
from app.schemas.base import PaginationParams
from app.services.articles import build_analyzed_article_preview


class WatchlistService:
    def __init__(
        self,
        user_id: UUID,
        repo: WatchlistRepository,
        article_repo: ArticleRepository,
    ) -> None:
        self.user_id = user_id
        self.repo = repo
        self.article_repo = article_repo

    async def list_articles_in_watchlist(
        self,
        pagination: PaginationParams,
    ) -> PaginatedArticleResponse:
        analyses, total = await self.repo.fetch_watched_articles(
            self.user_id, pagination
        )
        return PaginatedArticleResponse.create(
            items=[build_analyzed_article_preview(a) for a in analyses],
            total=total,
            pagination=pagination,
        )

    async def list_ids(self) -> list[int]:
        """ユーザーがウォッチ中の article_id を新しい順に返す。"""
        return await self.repo.list_ids(self.user_id)

    async def add_to_watchlist(self, article_id: int) -> bool:
        """新しく追加したときだけ True を返す。登録済みなら何もしない。"""
        if not await self.article_repo.exists_analyzed(article_id):
            raise NotFoundError("News article not found")
        return await self.repo.watch(self.user_id, article_id)

    async def remove_from_watchlist(self, article_id: int) -> None:
        await self.repo.unwatch(self.user_id, article_id)
