"""Queries for semantic search over analyzed articles."""

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
from app.schemas.articles import SemanticSearchParams, SortBy, SortOrder


class SemanticSearchRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def search_articles(
        self,
        query: SemanticSearchParams,
        query_embedding: list[float],
    ) -> tuple[list[NewsArticle], int]:
        """Search articles by semantic similarity with filters and pagination."""
        stmt = (
            select(NewsArticle)
            .join(ArticleAnalysis, ArticleAnalysis.news_article_id == NewsArticle.id)
            .options(
                selectinload(NewsArticle.article_analysis),
                selectinload(NewsArticle.news_source),
                selectinload(NewsArticle.article_keywords).selectinload(
                    ArticleKeyword.keyword
                ),
            )
        )

        # Embedding similarity filter
        stmt = stmt.where(ArticleAnalysis.embedding.is_not(None))
        distance_expr: ColumnElement[float] = ArticleAnalysis.embedding.cosine_distance(
            query_embedding
        )
        stmt = stmt.where(distance_expr < settings.semantic_search_max_distance)

        # Content filters
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
        total = await self._count(stmt)

        # Sort
        stmt = self._apply_sort(stmt, query.sort_by, query.sort_order, distance_expr)

        # Paginate
        offset = (query.page - 1) * query.per_page
        stmt = stmt.offset(offset).limit(query.per_page)

        result = await self.session.execute(stmt)
        return list(result.unique().scalars().all()), total

    async def _count(self, stmt: Select[Any]) -> int:
        count_stmt = select(func.count()).select_from(stmt.subquery())
        return (await self.session.execute(count_stmt)).scalar_one()

    @staticmethod
    def _apply_sort(
        stmt: Select[Any],
        sort_by: SortBy,
        sort_order: SortOrder,
        distance_expr: ColumnElement[float],
    ) -> Select[Any]:
        if sort_by == SortBy.RELEVANCE:
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
