"""Stage 5 embedding の DB 読み取りと永続化を担う repository。"""

from __future__ import annotations

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.analysis.embedding.domain.ready import EmbeddingReadyBuildFacts
from app.analysis.embedding.domain.value_objects import EmbeddingVector
from app.models.article_curation import ArticleCuration
from app.models.in_scope_assessment import InScopeAssessment


class EmbeddingRepository:
    """Domain 判断を持たず、DB 事実と保存結果だけを返す。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def load_ready_build_facts(
        self, analysis_id: int
    ) -> EmbeddingReadyBuildFacts | None:
        stmt = (
            select(
                ArticleCuration.article_id,
                InScopeAssessment.embedding.is_not(None),
                InScopeAssessment.translated_title,
                InScopeAssessment.summary,
            )
            .select_from(InScopeAssessment)
            .join(
                ArticleCuration,
                ArticleCuration.id == InScopeAssessment.curation_id,
            )
            .where(InScopeAssessment.id == analysis_id)
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
            update(InScopeAssessment)
            .where(
                InScopeAssessment.id == analysis_id,
                InScopeAssessment.embedding.is_(None),
            )
            .values(embedding=vector.to_list())
            .returning(InScopeAssessment.id)
        )
        row = (await self._session.execute(stmt)).first()
        return row is not None
