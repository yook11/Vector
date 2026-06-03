from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.news_source import NewsSource


class NewsSourceRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_all(self) -> list[NewsSource]:
        """name 順で全ニュースソースを取得する."""
        stmt = select(NewsSource).order_by(NewsSource.name)
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def get_by_id(self, source_id: int) -> NewsSource | None:
        """ID でニュースソースを 1 件取得する. 見つからなければ None."""
        return await self.session.get(NewsSource, source_id)

    async def create(self, source: NewsSource) -> None:
        """新規ニュースソースを永続化する. PK 採番のため flush する."""
        self.session.add(source)
        await self.session.flush()

    async def delete(self, source: NewsSource) -> None:
        """ニュースソースを削除する."""
        await self.session.delete(source)
