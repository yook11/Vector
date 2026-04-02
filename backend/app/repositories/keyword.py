from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from sqlmodel import func, select

from app.models.article_keyword import ArticleKeyword
from app.models.category import Category
from app.models.keyword import Keyword


class KeywordRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def fetch_all_with_stats(self) -> list[tuple[Keyword, int]]:
        """Fetch all keywords with category eager-loaded and article count."""
        stmt = (
            select(
                Keyword,
                func.count(ArticleKeyword.news_article_id).label("article_count"),
            )
            .outerjoin(ArticleKeyword, ArticleKeyword.keyword_id == Keyword.id)
            .options(selectinload(Keyword.category))
            .group_by(Keyword.id)
            .order_by(Keyword.created_at.desc())
        )
        result = await self.session.execute(stmt)
        return [(row[0], row[1]) for row in result.unique().all()]

    async def fetch_one_with_stats(self, keyword_id: int) -> tuple[Keyword, int]:
        """Fetch a single keyword with category and article count."""
        stmt = (
            select(
                Keyword,
                func.count(ArticleKeyword.news_article_id).label("article_count"),
            )
            .outerjoin(ArticleKeyword, ArticleKeyword.keyword_id == Keyword.id)
            .where(Keyword.id == keyword_id)
            .options(selectinload(Keyword.category))
            .group_by(Keyword.id)
        )
        result = await self.session.execute(stmt)
        row = result.unique().one()
        return row[0], row[1]

    async def get_by_id(self, keyword_id: int) -> Keyword | None:
        """Get a single keyword by ID."""
        return await self.session.get(Keyword, keyword_id)

    async def category_exists(self, category_id: int) -> bool:
        """Check whether a category ID is valid."""
        return await self.session.get(Category, category_id) is not None

    async def get_by_name(self, name: str) -> Keyword | None:
        """Get a keyword by name for uniqueness check."""
        stmt = select(Keyword).where(Keyword.name == name)
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def create(self, keyword: Keyword) -> Keyword:
        """Persist a new keyword and return the refreshed instance."""
        self.session.add(keyword)
        await self.session.commit()
        await self.session.refresh(keyword)
        return keyword

    async def delete(self, keyword: Keyword) -> None:
        """Delete a keyword."""
        await self.session.delete(keyword)
        await self.session.commit()

    async def save(self, keyword: Keyword) -> Keyword:
        """Persist changes to an existing keyword."""
        self.session.add(keyword)
        await self.session.commit()
        await self.session.refresh(keyword)
        return keyword
