from uuid import UUID

from sqlalchemy import delete, exists
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import func, select

from app.models.article_analysis import ArticleAnalysis
from app.models.article_extraction import ArticleExtraction
from app.models.watchlist_entry import WatchlistEntry
from app.repositories.articles import article_eager_options_brief
from app.schemas.base import PaginationParams


class WatchlistRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def fetch_watched_articles(
        self,
        user_id: UUID,
        pagination: PaginationParams,
    ) -> tuple[list[ArticleAnalysis], int]:
        """ウォッチ中の記事（分析済みのみ）をページングで取得する.

        (analyses, total_count) を返す.
        """
        base = (
            select(ArticleAnalysis)
            .join(ArticleAnalysis.extraction)
            .join(ArticleExtraction.article)
            .join(
                WatchlistEntry,
                WatchlistEntry.article_analysis_id == ArticleAnalysis.id,
            )
            .where(WatchlistEntry.user_id == user_id)
        )

        count_stmt = select(func.count()).select_from(base.subquery())
        total = (await self.session.execute(count_stmt)).scalar_one()

        stmt = (
            base.options(*article_eager_options_brief())
            .order_by(WatchlistEntry.created_at.desc())
            .offset(pagination.offset)
            .limit(pagination.limit)
        )
        result = await self.session.execute(stmt)
        analyses = list(result.unique().scalars().all())

        return analyses, total

    async def is_watched(self, user_id: UUID, article_id: int) -> bool:
        """ユーザーが当該記事を既にウォッチ中かを判定する."""
        stmt = select(
            exists().where(
                WatchlistEntry.user_id == user_id,
                WatchlistEntry.article_analysis_id == article_id,
            )
        )
        result = await self.session.execute(stmt)
        return result.scalar_one()

    async def watch(self, user_id: UUID, article_id: int) -> None:
        """ユーザーのウォッチリストに記事を追加する."""
        entry = WatchlistEntry(user_id=user_id, article_analysis_id=article_id)
        self.session.add(entry)

    async def watched_among(self, user_id: UUID, article_ids: set[int]) -> set[int]:
        """article_ids のうちユーザーがウォッチ中のものを返す.

        事前条件: article_ids は非空であること. 対象が空の場合は
        呼び出し側でこのメソッドの呼び出しをスキップする.
        """
        stmt = select(WatchlistEntry.article_analysis_id).where(
            WatchlistEntry.user_id == user_id,
            WatchlistEntry.article_analysis_id.in_(article_ids),
        )
        result = await self.session.execute(stmt)
        return set(result.scalars().all())

    async def unwatch(self, user_id: UUID, article_id: int) -> None:
        """ユーザーのウォッチリストから記事を削除する.

        存在チェックは呼び出し側の責務とする.
        """
        stmt = delete(WatchlistEntry).where(
            WatchlistEntry.user_id == user_id,
            WatchlistEntry.article_analysis_id == article_id,
        )
        await self.session.execute(stmt)
