from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import func, select

from app.models.article_analysis import ArticleAnalysis
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
        """Fetch paginated watched articles (analyzed only).

        Returns (analyses, total_count).
        """
        base = (
            select(ArticleAnalysis)
            .join(ArticleAnalysis.news_article)
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

    async def find_entry(self, user_id: UUID, article_id: int) -> WatchlistEntry | None:
        """Find a watchlist entry for the given user and article."""
        stmt = select(WatchlistEntry).where(
            WatchlistEntry.user_id == user_id,
            WatchlistEntry.article_analysis_id == article_id,
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def article_exists(self, article_id: int) -> bool:
        """Check whether an analyzed article exists."""
        stmt = select(ArticleAnalysis.id).where(ArticleAnalysis.id == article_id)
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none() is not None

    async def add_entry(self, user_id: UUID, article_id: int) -> None:
        """Create a new watchlist entry."""
        entry = WatchlistEntry(user_id=user_id, article_analysis_id=article_id)
        self.session.add(entry)
        await self.session.commit()

    async def get_watched_ids(self, user_id: UUID) -> set[int]:
        """Return set of article_analysis IDs in the user's watchlist."""
        stmt = select(WatchlistEntry.article_analysis_id).where(
            WatchlistEntry.user_id == user_id
        )
        result = await self.session.execute(stmt)
        return set(result.scalars().all())

    async def remove_entry(self, entry: WatchlistEntry) -> None:
        """Delete an existing watchlist entry."""
        await self.session.delete(entry)
        await self.session.commit()
