"""Read-only queries for articles (listing, detail, similar, watchlist)."""

from sqlalchemy import func, select, true
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.article_analysis import ArticleAnalysis
from app.models.article_keyword import ArticleKeyword
from app.models.category import Category
from app.models.keyword import Keyword
from app.models.news_article import NewsArticle
from app.models.news_source import NewsSource
from app.models.watchlist_entry import WatchlistEntry
from app.schemas.articles import ArticleListParams, SortOrder


def article_eager_options() -> list:
    """Selectinload options shared by article / watchlist queries."""
    return [
        selectinload(NewsArticle.article_analysis),
        selectinload(NewsArticle.news_source),
        selectinload(NewsArticle.article_keywords).selectinload(ArticleKeyword.keyword),
    ]


class ArticleRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    # -- public: listing ------------------------------------------------

    async def fetch_articles(
        self,
        query: ArticleListParams,
    ) -> tuple[list[NewsArticle], int]:
        """Fetch paginated article list for news browsing."""
        stmt = (
            select(NewsArticle)
            .join(ArticleAnalysis, ArticleAnalysis.news_article_id == NewsArticle.id)
            .options(*article_eager_options())
        )

        # Filters
        if query.source is not None:
            source_ids = select(NewsSource.id).where(NewsSource.name == query.source)
            stmt = stmt.where(NewsArticle.news_source_id.in_(source_ids))

        if query.keyword is not None:
            matching_ids = (
                select(ArticleKeyword.news_article_id)
                .join(Keyword, Keyword.id == ArticleKeyword.keyword_id)
                .where(Keyword.name == query.keyword)
            )
            stmt = stmt.where(NewsArticle.id.in_(matching_ids))
        elif query.category is not None:
            cat_id_sub = select(Category.id).where(Category.slug == query.category)
            sub_kw_ids = select(Keyword.id).where(Keyword.category_id.in_(cat_id_sub))
            matching_ids = select(ArticleKeyword.news_article_id).where(
                ArticleKeyword.keyword_id.in_(sub_kw_ids)
            )
            stmt = stmt.where(NewsArticle.id.in_(matching_ids))

        if query.impact_level is not None:
            stmt = stmt.where(ArticleAnalysis.impact_level == query.impact_level)

        # Count
        count_stmt = select(func.count()).select_from(stmt.subquery())
        total = (await self.session.execute(count_stmt)).scalar_one()

        # Sort
        order = (
            NewsArticle.published_at.desc()
            if query.sort_order == SortOrder.DESC
            else NewsArticle.published_at.asc()
        )
        stmt = stmt.order_by(order, NewsArticle.id.desc())

        # Paginate
        offset = (query.page - 1) * query.per_page
        stmt = stmt.offset(offset).limit(query.per_page)

        result = await self.session.execute(stmt)
        return list(result.unique().scalars().all()), total

    async def fetch_one_analyzed(self, news_id: int) -> NewsArticle | None:
        """Fetch a single article with analysis eager-loaded.

        Returns None if not found or not analyzed.
        """
        stmt = (
            select(NewsArticle)
            .where(NewsArticle.id == news_id)
            .options(*article_eager_options())
        )
        result = await self.session.execute(stmt)
        article = result.unique().scalar_one_or_none()
        if article is None or article.article_analysis is None:
            return None
        return article

    async def fetch_similar_to(self, news_id: int, limit: int) -> list[NewsArticle]:
        """Fetch articles similar to the given article, ordered by cosine distance.

        Returns an empty list when the article does not exist or has no embedding.
        """
        source_embedding = (
            select(ArticleAnalysis.embedding)
            .where(
                ArticleAnalysis.news_article_id == news_id,
                ArticleAnalysis.embedding.is_not(None),
            )
            .cte("source_embedding")
        )

        stmt = (
            select(NewsArticle)
            .join(ArticleAnalysis, ArticleAnalysis.news_article_id == NewsArticle.id)
            .join(source_embedding, true())
            .options(*article_eager_options())
            .where(
                NewsArticle.id != news_id,
                ArticleAnalysis.embedding.is_not(None),
            )
            .order_by(
                ArticleAnalysis.embedding.cosine_distance(source_embedding.c.embedding)
            )
            .limit(limit)
        )
        result = await self.session.execute(stmt)
        return list(result.unique().scalars().all())

    async def get_watched_ids(self, user_id: int) -> set[int]:
        """Return set of news_article_ids in the user's watchlist."""
        stmt = select(WatchlistEntry.news_article_id).where(
            WatchlistEntry.user_id == user_id
        )
        result = await self.session.execute(stmt)
        return set(result.scalars().all())
