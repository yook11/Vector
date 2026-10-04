from uuid import UUID

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.analyzed_article_record import AnalyzedArticleRecord
from app.models.article_curation import ArticleCuration
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
    ) -> tuple[list[AnalyzedArticleRecord], int]:
        """ウォッチ中の記事（分析済みのみ）をページングで取得する.

        (analyses, total_count) を返す.
        """
        base = (
            select(AnalyzedArticleRecord)
            .join(AnalyzedArticleRecord.curation)
            .join(ArticleCuration.analyzable_article)
            .join(
                WatchlistEntry,
                WatchlistEntry.analyzed_article_id == AnalyzedArticleRecord.id,
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

    async def list_ids(self, user_id: UUID) -> list[int]:
        """ユーザーがウォッチ中の analyzed_article_id を新しい順に返す."""
        stmt = (
            select(WatchlistEntry.analyzed_article_id)
            .where(WatchlistEntry.user_id == user_id)
            .order_by(WatchlistEntry.created_at.desc())
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def watch(self, user_id: UUID, article_id: int) -> bool:
        """ユーザーのウォッチリストに記事を追加する.

        同時に追加されても主キー重複にしないよう衝突は無視し、
        新しく追加したときだけ True を返す.
        """
        stmt = (
            pg_insert(WatchlistEntry)
            .values(user_id=user_id, analyzed_article_id=article_id)
            .on_conflict_do_nothing(index_elements=["user_id", "analyzed_article_id"])
            .returning(WatchlistEntry.analyzed_article_id)
        )
        row = (await self.session.execute(stmt)).first()
        return row is not None

    async def unwatch(self, user_id: UUID, article_id: int) -> None:
        """ユーザーのウォッチリストから記事を削除する. 未登録なら何もしない."""
        stmt = delete(WatchlistEntry).where(
            WatchlistEntry.user_id == user_id,
            WatchlistEntry.analyzed_article_id == article_id,
        )
        await self.session.execute(stmt)
