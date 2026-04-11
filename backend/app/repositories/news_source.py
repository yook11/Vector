from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.models.news_source import NewsSource


class NewsSourceRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_all(self) -> list[NewsSource]:
        """Get all news sources ordered by name."""
        stmt = select(NewsSource).order_by(NewsSource.name)
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def get_by_id(self, source_id: int) -> NewsSource | None:
        """Get a single news source by ID. Returns None if not found."""
        return await self.session.get(NewsSource, source_id)

    async def create(self, source: NewsSource) -> None:
        """Persist a new news source. Flushes to assign the primary key."""
        self.session.add(source)
        await self.session.flush()

    async def delete(self, source: NewsSource) -> None:
        """Delete a news source."""
        await self.session.delete(source)
