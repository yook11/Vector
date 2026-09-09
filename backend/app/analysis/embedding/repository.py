"""Stage 5 embedding の DB 読み取りと永続化を担う repository。"""

from __future__ import annotations

from enum import StrEnum

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.analysis.embedding.domain.ready import EmbeddingReadyBuildFacts
from app.analysis.embedding.domain.value_objects import EmbeddingVector
from app.models.analyzed_article_record import AnalyzedArticleRecord
from app.models.article_curation import ArticleCuration


class EmbeddingSaveState(StrEnum):
    """保存対象の存在とベクトルの生成状態。"""

    ARTICLE_MISSING = "article_missing"
    UNEMBEDDED = "unembedded"
    EMBEDDED = "embedded"


class EmbeddingRepository:
    """Domain 判断を持たず、DB 事実と保存結果だけを返す。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def load_ready_build_facts(
        self, analyzed_article_id: int
    ) -> EmbeddingReadyBuildFacts | None:
        stmt = (
            select(
                ArticleCuration.analyzable_article_id,
                AnalyzedArticleRecord.embedding.is_not(None),
                AnalyzedArticleRecord.summary,
                AnalyzedArticleRecord.key_points,
            )
            .select_from(AnalyzedArticleRecord)
            .join(
                ArticleCuration,
                ArticleCuration.id == AnalyzedArticleRecord.curation_id,
            )
            .where(AnalyzedArticleRecord.id == analyzed_article_id)
            .limit(1)
        )
        row = (await self._session.execute(stmt)).first()
        if row is None:
            return None
        analyzable_article_id, has_embedding, summary, key_points = row
        return EmbeddingReadyBuildFacts(
            analyzable_article_id=analyzable_article_id,
            has_embedding=has_embedding,
            summary=summary,
            key_points=key_points,
        )

    async def lock_save_state(self, analyzed_article_id: int) -> EmbeddingSaveState:
        """記事の行をトランザクション終了までロックし、保存前の状態を返す。"""
        stmt = (
            select(AnalyzedArticleRecord.embedding.is_not(None))
            .where(AnalyzedArticleRecord.id == analyzed_article_id)
            .with_for_update()
        )
        row = (await self._session.execute(stmt)).one_or_none()
        if row is None:
            return EmbeddingSaveState.ARTICLE_MISSING
        if row[0]:
            return EmbeddingSaveState.EMBEDDED
        return EmbeddingSaveState.UNEMBEDDED

    async def save(
        self,
        vector: EmbeddingVector,
        *,
        analyzed_article_id: int,
    ) -> bool:
        """未 embedded の analyzed article にだけ vector を保存する。"""
        stmt = (
            update(AnalyzedArticleRecord)
            .where(
                AnalyzedArticleRecord.id == analyzed_article_id,
                AnalyzedArticleRecord.embedding.is_(None),
            )
            .values(embedding=vector.to_list())
            .returning(AnalyzedArticleRecord.id)
        )
        row = (await self._session.execute(stmt)).first()
        return row is not None
