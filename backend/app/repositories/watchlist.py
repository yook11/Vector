from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import func, select

from app.models.article_analysis import ArticleAnalysis
from app.models.news_article import NewsArticle
from app.models.watchlist_entry import WatchlistEntry
from app.repositories.articles import article_eager_options
from app.schemas.base import PaginationParams


class WatchlistRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def fetch_watched_articles(
        self,
        user_id: int,
        pagination: PaginationParams,
    ) -> tuple[list[NewsArticle], int]:
        """Fetch paginated watched articles (analyzed only).

        Returns (articles, total_count).
        """
        base = (
            select(NewsArticle)
            .join(
                WatchlistEntry,
                WatchlistEntry.news_article_id == NewsArticle.id,
            )
            .join(
                ArticleAnalysis,
                ArticleAnalysis.news_article_id == NewsArticle.id,
            )
            .where(WatchlistEntry.user_id == user_id)
        )

        count_stmt = select(func.count()).select_from(base.subquery())
        total = (await self.session.execute(count_stmt)).scalar_one()

        stmt = (
            base.options(*article_eager_options())
            .order_by(WatchlistEntry.created_at.desc())
            .offset(pagination.offset)
            .limit(pagination.limit)
        )
        result = await self.session.execute(stmt)
        articles = list(result.unique().scalars().all())

        return articles, total

    async def find_entry(self, user_id: int, news_id: int) -> WatchlistEntry | None:
        """Find a watchlist entry for the given user and article."""
        stmt = select(WatchlistEntry).where(
            WatchlistEntry.user_id == user_id,
            WatchlistEntry.news_article_id == news_id,
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def article_exists(self, news_id: int) -> bool:
        """Check whether a news article exists."""
        stmt = select(NewsArticle.id).where(NewsArticle.id == news_id)
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none() is not None

    async def add_entry(self, user_id: int, news_id: int) -> None:
        """Create a new watchlist entry."""
        entry = WatchlistEntry(user_id=user_id, news_article_id=news_id)
        self.session.add(entry)
        await self.session.commit()

    async def get_watched_ids(self, user_id: int) -> set[int]:
        """Return set of news_article_ids in the user's watchlist."""
        stmt = select(WatchlistEntry.news_article_id).where(
            WatchlistEntry.user_id == user_id
        )
        result = await self.session.execute(stmt)
        return set(result.scalars().all())

    async def remove_entry(self, entry: WatchlistEntry) -> None:
        """Delete an existing watchlist entry."""
        await self.session.delete(entry)
        await self.session.commit()
