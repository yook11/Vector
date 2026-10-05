from datetime import datetime
from uuid import UUID

from sqlalchemy import delete, select, tuple_
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.analyzed_article_record import AnalyzedArticleRecord
from app.models.article_curation import ArticleCuration
from app.models.watchlist_entry import WatchlistEntry
from app.repositories.articles import article_eager_options_brief
from app.schemas.watchlist import WatchlistPosition


class WatchlistRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def fetch_watched_articles(
        self,
        user_id: UUID,
        after: WatchlistPosition | None,
        limit: int,
    ) -> list[tuple[AnalyzedArticleRecord, datetime]]:
        """ウォッチ中の記事（分析済みのみ）を、位置より後からウォッチの新しい順に取得する.

        各記事とウォッチした時刻の組を返す.
        """
        stmt = (
            select(AnalyzedArticleRecord, WatchlistEntry.created_at)
            .join(AnalyzedArticleRecord.curation)
            .join(ArticleCuration.analyzable_article)
            .join(
                WatchlistEntry,
                WatchlistEntry.analyzed_article_id == AnalyzedArticleRecord.id,
            )
            .where(WatchlistEntry.user_id == user_id)
            .options(*article_eager_options_brief())
        )

        if after is not None:
            stmt = stmt.where(
                tuple_(WatchlistEntry.created_at, WatchlistEntry.analyzed_article_id)
                < tuple_(after.watched_at, after.article_id)
            )

        stmt = stmt.order_by(
            WatchlistEntry.created_at.desc(),
            WatchlistEntry.analyzed_article_id.desc(),
        ).limit(limit)
        result = await self.session.execute(stmt)
        return [(analysis, watched_at) for analysis, watched_at in result.unique()]

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
