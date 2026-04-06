"""Read-only queries for analyzed articles."""

from typing import Any

from sqlalchemy import ColumnElement, Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import settings
from app.models.article_analysis import ArticleAnalysis
from app.models.article_keyword import ArticleKeyword
from app.models.category import Category
from app.models.keyword import Keyword
from app.models.news_article import NewsArticle
from app.models.news_source import NewsSource
from app.models.watchlist_entry import WatchlistEntry
from app.schemas.articles import ArticleListParams, SortBy, SortOrder


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

    # -- public ---------------------------------------------------------

    async def fetch_analyzed_list(
        self,
        query: ArticleListParams,
        query_embedding: list[float] | None = None,
    ) -> tuple[list[NewsArticle], int]:
        """Fetch paginated analyzed articles with filters and sorting.

        Returns (articles, total_count).
        """
        stmt, distance_expr = self._build_filtered_query(query, query_embedding)
        total = await self._count(stmt)
        stmt = self._apply_sort(stmt, query.sort_by, query.sort_order, distance_expr)
        stmt = self._apply_pagination(stmt, query.page, query.per_page)

        result = await self.session.execute(stmt)
        articles = list(result.unique().scalars().all())
        return articles, total

    # -- private --------------------------------------------------------

    def _build_filtered_query(
        self,
        query: ArticleListParams,
        query_embedding: list[float] | None,
    ) -> tuple[Select[Any], ColumnElement[float] | None]:
        """Build base query with all filters applied.

        Returns (statement, distance_expr). distance_expr is None when
        no semantic search is active.
        """
        stmt = (
            select(NewsArticle)
            .join(ArticleAnalysis, ArticleAnalysis.news_article_id == NewsArticle.id)
            .options(*article_eager_options())
        )

        distance_expr: ColumnElement[float] | None = None
        if query_embedding is not None:
            stmt = stmt.where(ArticleAnalysis.embedding.is_not(None))
            distance_expr = ArticleAnalysis.embedding.cosine_distance(query_embedding)
            stmt = stmt.where(distance_expr < settings.semantic_search_max_distance)

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

        return stmt, distance_expr

    @staticmethod
    def _apply_sort(
        stmt: Select[Any],
        sort_by: SortBy,
        sort_order: SortOrder,
        distance_expr: ColumnElement[float] | None,
    ) -> Select[Any]:
        if distance_expr is not None and sort_by == SortBy.RELEVANCE:
            return stmt.order_by(
                distance_expr.asc(),
                NewsArticle.published_at.desc(),
                NewsArticle.id.desc(),
            )
        order = (
            NewsArticle.published_at.desc()
            if sort_order == SortOrder.DESC
            else NewsArticle.published_at.asc()
        )
        return stmt.order_by(order, NewsArticle.id.desc())

    @staticmethod
    def _apply_pagination(stmt: Select[Any], page: int, per_page: int) -> Select[Any]:
        offset = (page - 1) * per_page
        return stmt.offset(offset).limit(per_page)

    async def _count(self, stmt: Select[Any]) -> int:
        count_stmt = select(func.count()).select_from(stmt.subquery())
        return (await self.session.execute(count_stmt)).scalar_one()

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

    async def get_analysis(self, news_id: int) -> ArticleAnalysis | None:
        """Get analysis for a given article (for similar-article lookup)."""
        stmt = select(ArticleAnalysis).where(ArticleAnalysis.news_article_id == news_id)
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def article_exists(self, news_id: int) -> bool:
        """Check whether an article exists."""
        stmt = select(NewsArticle.id).where(NewsArticle.id == news_id)
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none() is not None

    async def fetch_similar(
        self,
        embedding: list[float],
        exclude_id: int,
        limit: int,
    ) -> list[NewsArticle]:
        """Fetch articles similar to the given embedding, ordered by cosine distance."""
        stmt = (
            select(NewsArticle)
            .join(ArticleAnalysis, ArticleAnalysis.news_article_id == NewsArticle.id)
            .options(*article_eager_options())
            .where(
                NewsArticle.id != exclude_id,
                ArticleAnalysis.embedding.is_not(None),
            )
            .order_by(ArticleAnalysis.embedding.cosine_distance(embedding))
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
