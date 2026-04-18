from sqlalchemy import Row
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import func, select

from app.models.article_analysis import ArticleAnalysis
from app.models.category import Category
from app.models.topic import Topic


class CategoryRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def fetch_categories(self) -> list[Row[tuple[int, str, str]]]:
        """slug 順で全カテゴリを取得する.

        (id, slug, name) の行を返す.
        """
        stmt = select(Category.id, Category.slug, Category.name).order_by(Category.slug)
        result = await self.session.execute(stmt)
        return list(result.all())

    async def fetch_topic_stats(
        self,
    ) -> list[Row[tuple[int, str, int]]]:
        """カテゴリ別にトピックごとの記事数を取得する.

        (category_id, name, article_count) の行を返す.
        """
        stmt = (
            select(
                Topic.category_id,
                Topic.name,
                func.count(ArticleAnalysis.id).label("article_count"),
            )
            .outerjoin(ArticleAnalysis, ArticleAnalysis.topic_id == Topic.id)
            .group_by(Topic.category_id, Topic.id, Topic.name)
            .order_by(Topic.name)
        )
        result = await self.session.execute(stmt)
        return list(result.all())

    async def fetch_category_article_counts(
        self,
    ) -> list[Row[tuple[int, int]]]:
        """カテゴリごとのユニーク記事数を取得する.

        (category_id, article_count) の行を返す.
        Topic 経由で article_analyses を集計する。
        """
        stmt = (
            select(
                Topic.category_id,
                func.count(ArticleAnalysis.id).label("article_count"),
            )
            .join(ArticleAnalysis, ArticleAnalysis.topic_id == Topic.id)
            .group_by(Topic.category_id)
        )
        result = await self.session.execute(stmt)
        return list(result.all())
