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

    async def activate(self, source: NewsSource) -> NewsSource:
        """Mark a news source as active and persist the change."""
        source.is_active = True
        self.session.add(source)
        await self.session.commit()
        await self.session.refresh(source)
        return source

    async def deactivate(self, source: NewsSource) -> NewsSource:
        """Mark a news source as inactive and persist the change."""
        source.is_active = False
        self.session.add(source)
        await self.session.commit()
        await self.session.refresh(source)
        return source
