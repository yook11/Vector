from sqlalchemy import Row
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import func, select

from app.models.article_keyword import ArticleKeyword
from app.models.category import Category
from app.models.keyword import Keyword


class CategoryRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def fetch_categories(self) -> list[Row[tuple[int, str, str]]]:
        """Fetch all categories ordered by slug.

        Returns rows of (id, slug, name).
        """
        stmt = select(Category.id, Category.slug, Category.name).order_by(Category.slug)
        result = await self.session.execute(stmt)
        return list(result.all())

    async def fetch_keyword_stats(
        self,
    ) -> list[Row[tuple[int, int, str, int]]]:
        """Fetch per-keyword article counts grouped by category.

        Returns rows of (category_id, keyword_id, name, article_count).
        """
        stmt = (
            select(
                Keyword.category_id,
                Keyword.id.label("keyword_id"),
                Keyword.name,
                func.count(func.distinct(ArticleKeyword.news_article_id)).label(
                    "article_count"
                ),
            )
            .outerjoin(ArticleKeyword, ArticleKeyword.keyword_id == Keyword.id)
            .group_by(Keyword.category_id, Keyword.id, Keyword.name)
            .order_by(Keyword.name)
        )
        result = await self.session.execute(stmt)
        return list(result.all())

    async def fetch_category_article_counts(
        self,
    ) -> list[Row[tuple[int, int]]]:
        """Fetch per-category distinct article counts.

        Returns rows of (category_id, article_count).
        """
        stmt = (
            select(
                Keyword.category_id,
                func.count(func.distinct(ArticleKeyword.news_article_id)).label(
                    "article_count"
                ),
            )
            .join(ArticleKeyword, ArticleKeyword.keyword_id == Keyword.id)
            .group_by(Keyword.category_id)
        )
        result = await self.session.execute(stmt)
        return list(result.all())
