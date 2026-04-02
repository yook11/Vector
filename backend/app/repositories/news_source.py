from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import func, select

from app.models.news_source import NewsSource


class NewsSourceRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_all(self) -> list[NewsSource]:
        """Get all news sources ordered by name."""
        stmt = select(NewsSource).order_by(NewsSource.name)
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def get_count(self) -> int:
        """Get total count of news sources."""
        stmt = select(func.count()).select_from(NewsSource)
        return (await self.session.execute(stmt)).scalar_one()

    async def get_by_id(self, source_id: int) -> NewsSource | None:
        """Get a single news source by ID. Returns None if not found."""
        return await self.session.get(NewsSource, source_id)

    async def create(self, source: NewsSource) -> NewsSource:
        """Persist a new news source and return the refreshed instance."""
        self.session.add(source)
        await self.session.commit()
        await self.session.refresh(source)
        return source

    async def delete(self, source: NewsSource) -> None:
        """Delete a news source."""
        await self.session.delete(source)
        await self.session.commit()

    async def save(self, source: NewsSource) -> NewsSource:
        """Persist changes to an existing news source."""
        self.session.add(source)
        await self.session.commit()
        await self.session.refresh(source)
        return source
