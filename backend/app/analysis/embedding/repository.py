"""Stage 5 embedding の DB 読み取りと永続化を担う repository。"""

from __future__ import annotations

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.analysis.embedding.domain.ready import EmbeddingReadyBuildFacts
from app.analysis.embedding.domain.value_objects import EmbeddingVector
from app.models.analyzed_article_record import AnalyzedArticleRecord
from app.models.article_curation import ArticleCuration


class EmbeddingRepository:
    """Domain 判断を持たず、DB 事実と保存結果だけを返す。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def load_ready_build_facts(
        self, analysis_id: int
    ) -> EmbeddingReadyBuildFacts | None:
        stmt = (
            select(
                ArticleCuration.analyzable_article_id,
                AnalyzedArticleRecord.embedding.is_not(None),
                AnalyzedArticleRecord.translated_title,
                AnalyzedArticleRecord.summary,
            )
            .select_from(AnalyzedArticleRecord)
            .join(
                ArticleCuration,
                ArticleCuration.id == AnalyzedArticleRecord.curation_id,
            )
            .where(AnalyzedArticleRecord.id == analysis_id)
            .limit(1)
        )
        row = (await self._session.execute(stmt)).first()
        if row is None:
            return None
        article_id, has_embedding, translated_title, summary = row
        return EmbeddingReadyBuildFacts(
            article_id=article_id,
            has_embedding=has_embedding,
            translated_title=translated_title,
            summary=summary,
        )

    async def save(
        self,
        vector: EmbeddingVector,
        *,
        analysis_id: int,
    ) -> bool:
        """未 embedded の analysis にだけ vector を保存する。"""
        stmt = (
            update(AnalyzedArticleRecord)
            .where(
                AnalyzedArticleRecord.id == analysis_id,
                AnalyzedArticleRecord.embedding.is_(None),
            )
            .values(embedding=vector.to_list())
            .returning(AnalyzedArticleRecord.id)
        )
        row = (await self._session.execute(stmt)).first()
        return row is not None
