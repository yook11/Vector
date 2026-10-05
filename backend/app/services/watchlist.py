from uuid import UUID

from app.exceptions import NotFoundError
from app.repositories.articles import ArticleRepository
from app.repositories.watchlist import WatchlistRepository
from app.schemas.articles import ARTICLE_LIST_LIMIT, AnalyzedArticlePreviewList
from app.schemas.watchlist import WatchlistParams, WatchlistPosition
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
        params: WatchlistParams,
    ) -> AnalyzedArticlePreviewList:
        """ウォッチ中の記事を1回分取得する。1件多く読んで続きの有無を判定する。"""
        rows = await self.repo.fetch_watched_articles(
            self.user_id, params.cursor, limit=ARTICLE_LIST_LIMIT + 1
        )
        page = rows[:ARTICLE_LIST_LIMIT]
        next_cursor = None
        if len(rows) > ARTICLE_LIST_LIMIT:
            last, watched_at = page[-1]
            next_cursor = WatchlistPosition(
                watched_at=watched_at, article_id=last.id
            ).to_cursor()
        return AnalyzedArticlePreviewList(
            items=[build_analyzed_article_preview(a) for a, _ in page],
            next_cursor=next_cursor,
        )

    async def list_watched_ids(self, article_ids: list[int]) -> list[int]:
        """渡した記事のうち、ウォッチ中のものの ID を返す。"""
        return await self.repo.fetch_watched_ids(self.user_id, article_ids)

    async def add_to_watchlist(self, article_id: int) -> bool:
        """新しく追加したときだけ True を返す。登録済みなら何もしない。"""
        if not await self.article_repo.exists_analyzed(article_id):
            raise NotFoundError("News article not found")
        return await self.repo.watch(self.user_id, article_id)

    async def remove_from_watchlist(self, article_id: int) -> None:
        await self.repo.unwatch(self.user_id, article_id)
