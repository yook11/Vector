"""Queries for pipeline operations (embedding backfill, etc.)."""

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.models.article_analysis import ArticleAnalysis


class PipelineRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_analyses_without_embedding(self) -> list[ArticleAnalysis]:
        """Get all analyses that lack an embedding vector."""
        stmt = select(ArticleAnalysis).where(ArticleAnalysis.embedding.is_(None))
        result = await self.session.execute(stmt)
        return list(result.scalars().all())
