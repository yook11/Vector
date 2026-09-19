"""Stage 3 curation の DB 読み取りと永続化を担う repository。"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.analysis.curation.ai.envelope import CurationCall
from app.analysis.curation.domain import Noise, Signal
from app.analysis.curation.domain.ready import CurationReadyBuildFacts
from app.models.analyzable_article_record import AnalyzableArticleRecord
from app.models.article_curation import ArticleCuration
from app.models.curation_noise import CurationNoise


class CurationRepository:
    """Domain 判断を持たず、DB 事実と保存結果だけを返す。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # ------------------------------------------------------------------
    # Ready 構築用 DB 事実取得
    # ------------------------------------------------------------------

    async def load_ready_build_facts(
        self, analyzable_article_id: int
    ) -> CurationReadyBuildFacts | None:
        stmt = (
            select(
                AnalyzableArticleRecord.id,
                AnalyzableArticleRecord.original_title,
                AnalyzableArticleRecord.original_content,
                ArticleCuration.id.is_not(None),
                CurationNoise.id.is_not(None),
            )
            .select_from(AnalyzableArticleRecord)
            .outerjoin(
                ArticleCuration,
                ArticleCuration.analyzable_article_id == AnalyzableArticleRecord.id,
            )
            .outerjoin(
                CurationNoise,
                CurationNoise.analyzable_article_id == AnalyzableArticleRecord.id,
            )
            .where(AnalyzableArticleRecord.id == analyzable_article_id)
            .limit(1)
        )
        row = (await self._session.execute(stmt)).first()
        if row is None:
            return None
        (
            loaded_analyzable_article_id,
            original_title,
            original_content,
            has_signal_curation,
            has_noise_curation,
        ) = row
        return CurationReadyBuildFacts(
            analyzable_article_id=loaded_analyzable_article_id,
            original_title=original_title,
            original_content=original_content,
            has_signal_curation=has_signal_curation,
            has_noise_curation=has_noise_curation,
        )

    # ------------------------------------------------------------------
    # signal path
    # ------------------------------------------------------------------

    async def save_signal(
        self,
        call: CurationCall[Signal],
        *,
        analyzable_article_id: int,
    ) -> int | None:
        """signal を保存し、既存行に負けた場合は ``None`` を返す。"""
        signal = call.result
        stmt = (
            pg_insert(ArticleCuration)
            .values(
                analyzable_article_id=analyzable_article_id,
                translated_title=signal.title_ja,
                summary=signal.summary_ja,
            )
            .on_conflict_do_nothing(index_elements=["analyzable_article_id"])
            .returning(ArticleCuration.id)
        )
        return (await self._session.execute(stmt)).scalar()

    async def save_noise(
        self,
        call: CurationCall[Noise],
        *,
        analyzable_article_id: int,
    ) -> int | None:
        """noise を保存し、既存行に負けた場合は ``None`` を返す。"""
        noise = call.result
        stmt = (
            pg_insert(CurationNoise)
            .values(
                analyzable_article_id=analyzable_article_id,
                translated_title=noise.title_ja,
                summary=noise.summary_ja,
            )
            .on_conflict_do_nothing()
            .returning(CurationNoise.id)
        )
        return (await self._session.execute(stmt)).scalar()
