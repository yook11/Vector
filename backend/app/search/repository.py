"""分析済み記事に対するセマンティック検索クエリ。"""

from typing import Any

from sqlalchemy import ColumnElement, Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.article_analysis import ArticleAnalysis
from app.models.category import Category
from app.models.news_article import NewsArticle
from app.models.topic import Topic
from app.repositories.articles import article_eager_options_brief
from app.schemas.articles import SemanticSearchParams, SortBy, SortOrder


class SemanticSearchRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def search_articles(
        self,
        query: SemanticSearchParams,
        query_embedding: list[float],
    ) -> tuple[list[ArticleAnalysis], int]:
        """セマンティック類似度に基づき記事を検索する (フィルタ+ページング付き)。"""
        stmt = (
            select(ArticleAnalysis)
            .join(ArticleAnalysis.news_article)
            .options(*article_eager_options_brief())
        )

        # Stage 2 未完了の記事を除外 + Embedding 類似度フィルタ
        stmt = stmt.where(
            ArticleAnalysis.topic_id.is_not(None),
            ArticleAnalysis.embedding.is_not(None),
        )
        distance_expr: ColumnElement[float] = ArticleAnalysis.embedding.cosine_distance(
            query_embedding
        )
        stmt = stmt.where(distance_expr < settings.semantic_search_max_distance)

        # コンテンツフィルタ
        if query.topic is not None:
            topic_id_sub = select(Topic.id).where(Topic.name == query.topic)
            stmt = stmt.where(ArticleAnalysis.topic_id.in_(topic_id_sub))
        elif query.category is not None:
            cat_id_sub = select(Category.id).where(Category.slug == query.category)
            topic_id_sub = select(Topic.id).where(Topic.category_id.in_(cat_id_sub))
            stmt = stmt.where(ArticleAnalysis.topic_id.in_(topic_id_sub))

        if query.impact_level is not None:
            stmt = stmt.where(ArticleAnalysis.impact_level == query.impact_level)

        # 件数取得
        total = await self._count(stmt)

        # ソート
        stmt = self._apply_sort(stmt, query.sort_by, query.sort_order, distance_expr)

        # ページング
        stmt = stmt.offset(query.offset).limit(query.limit)

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
                ArticleAnalysis.id.desc(),
            )
        order = (
            NewsArticle.published_at.desc()
            if sort_order == SortOrder.DESC
            else NewsArticle.published_at.asc()
        )
        return stmt.order_by(order, ArticleAnalysis.id.desc())
