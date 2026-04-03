"""Read-only queries for analyzed articles."""

from typing import Annotated

from fastapi import HTTPException, Query, status
from sqlalchemy import case
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from sqlmodel import func, select

from app.config import settings
from app.domain.category import CategorySlug
from app.models.article_analysis import ArticleAnalysis, ImpactLevel
from app.models.article_keyword import ArticleKeyword
from app.models.category import Category
from app.models.keyword import Keyword
from app.models.news_article import NewsArticle
from app.models.news_source import NewsSource
from app.models.watchlist_entry import WatchlistEntry

_impact_order_expr = case(
    (ArticleAnalysis.impact_level == ImpactLevel.LOW, 1),
    (ArticleAnalysis.impact_level == ImpactLevel.MEDIUM, 2),
    (ArticleAnalysis.impact_level == ImpactLevel.HIGH, 3),
    (ArticleAnalysis.impact_level == ImpactLevel.CRITICAL, 4),
    else_=0,
)

_IMPACT_LEVEL_ORDER = {
    ImpactLevel.LOW: 1,
    ImpactLevel.MEDIUM: 2,
    ImpactLevel.HIGH: 3,
    ImpactLevel.CRITICAL: 4,
}


def article_eager_options() -> list:
    """Selectinload options shared by article / watchlist queries."""
    return [
        selectinload(NewsArticle.article_analysis),
        selectinload(NewsArticle.news_source),
        selectinload(NewsArticle.article_keywords)
        .selectinload(ArticleKeyword.keyword)
        .selectinload(Keyword.category),
    ]


class ArticleListParams:
    """Filter / sort / pagination parameters for article list query.

    Usable as a FastAPI dependency via ``Depends()``.
    """

    __slots__ = (
        "keyword_id",
        "category_slug",
        "source_name",
        "impact_level",
        "sort_by",
        "sort_order",
        "page",
        "per_page",
    )

    def __init__(
        self,
        keyword_id: Annotated[int | None, Query(alias="keywordId")] = None,
        category: Annotated[str | None, Query()] = None,
        source: Annotated[str | None, Query()] = None,
        impact_level: Annotated[ImpactLevel | None, Query(alias="impactLevel")] = None,
        sort_by: Annotated[str, Query(alias="sortBy")] = "publishedAt",
        sort_order: Annotated[str, Query(alias="sortOrder")] = "desc",
        page: Annotated[int, Query(ge=1)] = 1,
        per_page: Annotated[int, Query(ge=1, le=100, alias="perPage")] = 12,
    ) -> None:
        try:
            self.category_slug = CategorySlug(category) if category else None
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Invalid category slug: {category!r}",
            )
        self.keyword_id = keyword_id
        self.source_name = source
        self.impact_level = impact_level
        self.sort_by = sort_by
        self.sort_order = sort_order
        self.page = page
        self.per_page = per_page


class ArticleRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def fetch_analyzed_list(
        self,
        params: ArticleListParams,
        query_embedding: list[float] | None = None,
    ) -> tuple[list[NewsArticle], int]:
        """Fetch paginated analyzed articles with filters and sorting.

        Returns (articles, total_count).
        """
        stmt = (
            select(NewsArticle)
            .join(ArticleAnalysis, ArticleAnalysis.news_article_id == NewsArticle.id)
            .options(*article_eager_options())
        )

        # Semantic search filter
        if query_embedding is not None:
            stmt = stmt.where(ArticleAnalysis.embedding.is_not(None))
            distance_expr = ArticleAnalysis.embedding.cosine_distance(query_embedding)
            stmt = stmt.where(distance_expr < settings.semantic_search_max_distance)

        if params.source_name is not None:
            source_ids = select(NewsSource.id).where(
                NewsSource.name == params.source_name
            )
            stmt = stmt.where(NewsArticle.news_source_id.in_(source_ids))

        if params.keyword_id is not None:
            matching_ids = select(ArticleKeyword.news_article_id).where(
                ArticleKeyword.keyword_id == params.keyword_id
            )
            stmt = stmt.where(NewsArticle.id.in_(matching_ids))
        elif params.category_slug is not None:
            cat_id_sub = select(Category.id).where(
                Category.slug == params.category_slug
            )
            sub_kw_ids = select(Keyword.id).where(Keyword.category_id.in_(cat_id_sub))
            matching_ids = select(ArticleKeyword.news_article_id).where(
                ArticleKeyword.keyword_id.in_(sub_kw_ids)
            )
            stmt = stmt.where(NewsArticle.id.in_(matching_ids))

        if params.impact_level is not None:
            min_order = _IMPACT_LEVEL_ORDER[params.impact_level]
            stmt = stmt.where(_impact_order_expr >= min_order)

        # Total count before pagination
        count_stmt = select(func.count()).select_from(stmt.subquery())
        total = (await self.session.execute(count_stmt)).scalar_one()

        # Sorting
        is_default_sort = (
            params.sort_by == "publishedAt" and params.sort_order == "desc"
        )
        if query_embedding is not None and is_default_sort:
            distance_expr = ArticleAnalysis.embedding.cosine_distance(query_embedding)
            stmt = stmt.order_by(distance_expr.asc())
        else:
            if params.sort_by == "impactLevel":
                order_expr = _impact_order_expr
            else:
                order_expr = NewsArticle.published_at
            stmt = stmt.order_by(
                order_expr.desc() if params.sort_order == "desc" else order_expr.asc()
            )

        # Pagination
        offset = (params.page - 1) * params.per_page
        stmt = stmt.offset(offset).limit(params.per_page)

        result = await self.session.execute(stmt)
        articles = list(result.unique().scalars().all())
        return articles, total

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
