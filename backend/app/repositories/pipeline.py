"""Queries for pipeline operations."""

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.models.article_analysis import ArticleAnalysis
from app.models.news_article import NewsArticle


class PipelineRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_article_ids_without_embedding(self) -> list[int]:
        """Get article IDs that have an analysis but no embedding vector."""
        stmt = (
            select(NewsArticle.id)
            .join(
                ArticleAnalysis,
                ArticleAnalysis.news_article_id == NewsArticle.id,
            )
            .where(ArticleAnalysis.embedding.is_(None))
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())
