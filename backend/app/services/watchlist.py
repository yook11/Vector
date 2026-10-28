from uuid import UUID

from app.exceptions import NotFoundError, NotFoundResource
from app.repositories.articles import ArticleRepository
from app.repositories.watchlist import WatchlistRepository
from app.schemas.articles import ARTICLE_PAGE_SIZE, AnalyzedArticlePreviewList
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
        watched = await self.repo.fetch_watched_articles(
            self.user_id, params.cursor, limit=ARTICLE_PAGE_SIZE + 1
        )
        page = watched[:ARTICLE_PAGE_SIZE]
        next_cursor = None
        if len(watched) > ARTICLE_PAGE_SIZE:
            last_article, last_watched_at = page[-1]
            next_cursor = WatchlistPosition(
                watched_at=last_watched_at, article_id=last_article.id
            ).to_cursor()
        return AnalyzedArticlePreviewList(
            items=[build_analyzed_article_preview(article) for article, _ in page],
            next_cursor=next_cursor,
        )

    async def list_watched_ids(self, article_ids: list[int]) -> list[int]:
        """渡した記事のうち、ウォッチ中のものの ID を返す。"""
        return await self.repo.fetch_watched_ids(self.user_id, article_ids)

    async def add_to_watchlist(self, article_id: int) -> bool:
        """新しく追加したときだけ True を返す。登録済みなら何もしない。"""
        if not await self.article_repo.exists_analyzed(article_id):
            raise NotFoundError(NotFoundResource.NEWS_ARTICLE)
        return await self.repo.watch(self.user_id, article_id)

    async def remove_from_watchlist(self, article_id: int) -> None:
        await self.repo.unwatch(self.user_id, article_id)
