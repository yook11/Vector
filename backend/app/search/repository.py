"""分析済み記事に対するセマンティック検索クエリ。"""

from typing import Any

from sqlalchemy import ColumnElement, Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.article import Article
from app.models.article_extraction import ArticleExtraction
from app.models.category import Category
from app.models.in_scope_assessment import InScopeAssessment
from app.repositories.articles import article_eager_options_brief
from app.schemas.articles import SemanticSearchParams, SortBy, SortOrder


class SemanticSearchRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def search_articles(
        self,
        query: SemanticSearchParams,
        query_embedding: list[float],
    ) -> tuple[list[InScopeAssessment], int]:
        """セマンティック類似度に基づき記事を検索する (フィルタ+ページング付き)。"""
        stmt = (
            select(InScopeAssessment)
            .join(InScopeAssessment.extraction)
            .join(ArticleExtraction.article)
            .options(*article_eager_options_brief())
        )

        # Embedding が未生成の記事は検索対象外
        stmt = stmt.where(InScopeAssessment.embedding.is_not(None))
        distance_expr: ColumnElement[float] = (
            InScopeAssessment.embedding.cosine_distance(query_embedding)
        )
        stmt = stmt.where(distance_expr < settings.semantic_search_max_distance)

        # コンテンツフィルタ
        if query.category is not None:
            cat_id_sub = select(Category.id).where(Category.slug == query.category)
            stmt = stmt.where(InScopeAssessment.category_id.in_(cat_id_sub))

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
                Article.published_at.desc(),
                InScopeAssessment.id.desc(),
            )
        order = (
            Article.published_at.desc()
            if sort_order == SortOrder.DESC
            else Article.published_at.asc()
        )
        return stmt.order_by(order, InScopeAssessment.id.desc())
