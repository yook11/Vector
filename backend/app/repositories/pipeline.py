"""パイプライン処理向けのクエリ群."""

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.models.article import Article
from app.models.article_analysis import ArticleAnalysis
from app.models.article_extraction import ArticleExtraction


class PipelineRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_article_ids_without_embedding(self) -> list[int]:
        """分析済みだが embedding 未生成の記事 ID を取得する."""
        stmt = (
            select(Article.id)
            .join(ArticleExtraction, ArticleExtraction.article_id == Article.id)
            .join(
                ArticleAnalysis,
                ArticleAnalysis.extraction_id == ArticleExtraction.id,
            )
            .where(ArticleAnalysis.embedding.is_(None))
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())
