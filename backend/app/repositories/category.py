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
        """slug 順で全カテゴリを取得する.

        (id, slug, name) の行を返す.
        """
        stmt = select(Category.id, Category.slug, Category.name).order_by(Category.slug)
        result = await self.session.execute(stmt)
        return list(result.all())

    async def fetch_keyword_stats(
        self,
    ) -> list[Row[tuple[int, str, int]]]:
        """カテゴリ別にキーワードごとの記事数を取得する.

        (category_id, name, article_count) の行を返す.
        """
        stmt = (
            select(
                Keyword.category_id,
                Keyword.name,
                func.count(func.distinct(ArticleKeyword.article_analysis_id)).label(
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
        """カテゴリごとのユニーク記事数を取得する.

        (category_id, article_count) の行を返す.
        """
        stmt = (
            select(
                Keyword.category_id,
                func.count(func.distinct(ArticleKeyword.article_analysis_id)).label(
                    "article_count"
                ),
            )
            .join(ArticleKeyword, ArticleKeyword.keyword_id == Keyword.id)
            .group_by(Keyword.category_id)
        )
        result = await self.session.execute(stmt)
        return list(result.all())
