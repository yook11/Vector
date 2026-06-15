"""Stage 3 curation の DB 読み取りと永続化を担う repository。"""

from __future__ import annotations

from sqlalchemy import func, select, update
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

    async def signal_exists_for_article(self, analyzable_article_id: int) -> bool:
        """signal curation が既に存在するかを返す。"""
        stmt = (
            select(ArticleCuration.id)
            .where(ArticleCuration.analyzable_article_id == analyzable_article_id)
            .limit(1)
        )
        return (await self._session.execute(stmt)).first() is not None

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

    async def update_signal_idempotent(
        self,
        call: CurationCall[Signal],
        *,
        analyzable_article_id: int,
    ) -> int:
        """既存の Extraction を新しい ``CurationCall[Signal]`` で上書きする (CLI 用)。

        ``CurationCall[Signal]`` のみ受け付ける型 narrow により、Noise を
        signal table に上書きする経路を構造的に排除する
        (``feedback_structural_guarantee``)。

        Phase 1B α-1 の re-curation CLI 専用。再現性を持たせるため:

        - 親 ``ArticleCuration`` は **UPDATE のみ** (DELETE しない)。これにより
          ``analyzed_articles`` / ``out_of_scope_articles`` /
          ``article_embeddings`` / ``watchlist_entries`` への CASCADE 連鎖を
          構造的に回避する
          (parent DELETE するとユーザの watchlist が消失するため)。
        - ``extracted_at`` は ``func.now()`` で再採番する (再抽出した時刻として
          扱い、後段の運用で「いつ抽出された」を取り違えない)。

        対象 analyzable_article_id に対する curation が存在しない前提 (CLI 側で事前に
        ``signal_exists_for_article`` で絞り込む)。存在しない場合は
        ``NoResultFound``。

        Returns:
            更新された ``article_curations.id`` (parent UPDATE のみで id は不変)
        """
        signal = call.result
        update_stmt = (
            update(ArticleCuration)
            .where(ArticleCuration.analyzable_article_id == analyzable_article_id)
            .values(
                translated_title=signal.title_ja,
                summary=signal.summary_ja,
                extracted_at=func.now(),
            )
            .returning(ArticleCuration.id)
        )
        curation_id = (await self._session.execute(update_stmt)).scalar_one()
        await self._session.flush()

        return curation_id

    # ------------------------------------------------------------------
    # noise path
    # ------------------------------------------------------------------

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
