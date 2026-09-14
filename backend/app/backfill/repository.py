"""新 backfill の対象抽出と期限切れ再確認を担う。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import Select, func, select, true
from sqlalchemy.ext.asyncio import AsyncSession

from app.analysis.assessment.events import ArticleAssessedInScope
from app.analysis.curation.events import ArticleCuratedSignal
from app.backfill.targets import BackfillEventTarget, BackfillTarget
from app.collection.events import AnalyzableArticleCreated
from app.models.analyzable_article_record import AnalyzableArticleRecord
from app.models.analyzed_article_record import AnalyzedArticleRecord
from app.models.article_curation import ArticleCuration
from app.models.backfill_exclusion import (
    AssessmentBackfillExclusion,
    EmbeddingBackfillExclusion,
)
from app.models.curation_noise import CurationNoise
from app.models.news_source import NewsSource
from app.models.out_of_scope_article_record import OutOfScopeArticleRecord


class PipelineBacklog:
    """SQS 再投入と期限切れ整理の対象取得・件数観測を担う。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def _curation_pending(
        self,
        stmt: Select[Any],
        *,
        created_before: datetime,
        created_after: datetime | None = None,
    ) -> Select[Any]:
        return (
            stmt.select_from(AnalyzableArticleRecord)
            .outerjoin(
                ArticleCuration,
                ArticleCuration.analyzable_article_id == AnalyzableArticleRecord.id,
            )
            .outerjoin(
                CurationNoise,
                CurationNoise.analyzable_article_id == AnalyzableArticleRecord.id,
            )
            .where(
                ArticleCuration.id.is_(None),
                CurationNoise.id.is_(None),
                AnalyzableArticleRecord.created_at < created_before,
                (
                    AnalyzableArticleRecord.created_at >= created_after
                    if created_after is not None
                    else true()
                ),
            )
        )

    def _assessment_pending(
        self,
        stmt: Select[Any],
        *,
        created_before: datetime,
        created_after: datetime | None = None,
    ) -> Select[Any]:
        return (
            stmt.select_from(ArticleCuration)
            .join(
                AnalyzableArticleRecord,
                AnalyzableArticleRecord.id == ArticleCuration.analyzable_article_id,
            )
            .outerjoin(
                AnalyzedArticleRecord,
                AnalyzedArticleRecord.curation_id == ArticleCuration.id,
            )
            .outerjoin(
                OutOfScopeArticleRecord,
                OutOfScopeArticleRecord.curation_id == ArticleCuration.id,
            )
            .outerjoin(
                AssessmentBackfillExclusion,
                AssessmentBackfillExclusion.curation_id == ArticleCuration.id,
            )
            .where(
                AnalyzedArticleRecord.id.is_(None),
                OutOfScopeArticleRecord.id.is_(None),
                AssessmentBackfillExclusion.curation_id.is_(None),
                AnalyzableArticleRecord.created_at < created_before,
                (
                    AnalyzableArticleRecord.created_at >= created_after
                    if created_after is not None
                    else true()
                ),
            )
        )

    def _embedding_pending(
        self,
        stmt: Select[Any],
        *,
        created_before: datetime,
        created_after: datetime | None = None,
    ) -> Select[Any]:
        return (
            stmt.select_from(AnalyzedArticleRecord)
            .join(
                ArticleCuration,
                ArticleCuration.id == AnalyzedArticleRecord.curation_id,
            )
            .join(
                AnalyzableArticleRecord,
                AnalyzableArticleRecord.id == ArticleCuration.analyzable_article_id,
            )
            .outerjoin(
                EmbeddingBackfillExclusion,
                EmbeddingBackfillExclusion.analyzed_article_id
                == AnalyzedArticleRecord.id,
            )
            .where(
                AnalyzedArticleRecord.embedding.is_(None),
                EmbeddingBackfillExclusion.analyzed_article_id.is_(None),
                AnalyzableArticleRecord.created_at < created_before,
                (
                    AnalyzableArticleRecord.created_at >= created_after
                    if created_after is not None
                    else true()
                ),
            )
        )

    async def curation_events_pending(
        self,
        *,
        created_before: datetime,
        created_after: datetime,
        limit: int,
    ) -> list[BackfillEventTarget]:
        """curationの再投入payloadと監査主語を同じ照会で取得する。"""
        stmt = (
            self._curation_pending(
                select(
                    AnalyzableArticleRecord.id,
                    AnalyzableArticleRecord.id,
                    NewsSource.name,
                    AnalyzableArticleRecord.created_at,
                ),
                created_before=created_before,
                created_after=created_after,
            )
            .outerjoin(NewsSource, NewsSource.id == AnalyzableArticleRecord.source_id)
            .order_by(
                AnalyzableArticleRecord.created_at.asc(),
                AnalyzableArticleRecord.id.asc(),
            )
            .limit(limit)
        )
        rows = (await self._session.execute(stmt)).tuples().all()
        return [
            BackfillEventTarget(
                target=BackfillTarget(target_id, article_id, source_name),
                occurred_at=occurred_at,
                payload=AnalyzableArticleCreated(analyzable_article_id=target_id),
            )
            for target_id, article_id, source_name, occurred_at in rows
        ]

    async def lock_aged_out_curation(
        self,
        target_id: int,
        *,
        created_before: datetime,
    ) -> int | None:
        """行ロック後の新しい照会で期限切れと未完了を再確認する。"""
        locked = await self._session.scalar(
            select(AnalyzableArticleRecord.id)
            .where(AnalyzableArticleRecord.id == target_id)
            .with_for_update()
        )
        if locked is None:
            return None
        stmt = self._curation_pending(
            select(AnalyzableArticleRecord.id),
            created_before=created_before,
        ).where(AnalyzableArticleRecord.id == target_id)
        return await self._session.scalar(stmt)

    async def assessment_events_pending(
        self,
        *,
        created_before: datetime,
        created_after: datetime,
        limit: int,
    ) -> list[BackfillEventTarget]:
        """assessmentの再投入payloadと監査主語を同じ照会で取得する。"""
        stmt = (
            self._assessment_pending(
                select(
                    ArticleCuration.id,
                    AnalyzableArticleRecord.id,
                    NewsSource.name,
                    ArticleCuration.extracted_at,
                ),
                created_before=created_before,
                created_after=created_after,
            )
            .outerjoin(NewsSource, NewsSource.id == AnalyzableArticleRecord.source_id)
            .order_by(
                AnalyzableArticleRecord.created_at.asc(), ArticleCuration.id.asc()
            )
            .limit(limit)
        )
        rows = (await self._session.execute(stmt)).tuples().all()
        return [
            BackfillEventTarget(
                target=BackfillTarget(target_id, article_id, source_name),
                occurred_at=occurred_at,
                payload=ArticleCuratedSignal(
                    analyzable_article_id=article_id, curation_id=target_id
                ),
            )
            for target_id, article_id, source_name, occurred_at in rows
        ]

    async def lock_aged_out_assessment(
        self,
        target_id: int,
        *,
        created_before: datetime,
    ) -> int | None:
        """行ロック後の新しい照会で期限切れと未完了を再確認する。"""
        locked = await self._session.scalar(
            select(ArticleCuration.id)
            .where(ArticleCuration.id == target_id)
            .with_for_update()
        )
        if locked is None:
            return None
        stmt = self._assessment_pending(
            select(AnalyzableArticleRecord.id),
            created_before=created_before,
        ).where(ArticleCuration.id == target_id)
        return await self._session.scalar(stmt)

    async def embedding_events_pending(
        self,
        *,
        created_before: datetime,
        created_after: datetime,
        limit: int,
    ) -> list[BackfillEventTarget]:
        """embeddingの再投入payloadと監査主語を同じ照会で取得する。"""
        stmt = (
            self._embedding_pending(
                select(
                    AnalyzedArticleRecord.id,
                    AnalyzableArticleRecord.id,
                    NewsSource.name,
                    AnalyzedArticleRecord.analyzed_at,
                    AnalyzedArticleRecord.curation_id,
                ),
                created_before=created_before,
                created_after=created_after,
            )
            .outerjoin(NewsSource, NewsSource.id == AnalyzableArticleRecord.source_id)
            .order_by(
                AnalyzableArticleRecord.created_at.asc(), AnalyzedArticleRecord.id.asc()
            )
            .limit(limit)
        )
        rows = (await self._session.execute(stmt)).tuples().all()
        return [
            BackfillEventTarget(
                target=BackfillTarget(target_id, article_id, source_name),
                occurred_at=occurred_at,
                payload=ArticleAssessedInScope(
                    curation_id=curation_id, analyzed_article_id=target_id
                ),
            )
            for target_id, article_id, source_name, occurred_at, curation_id in rows
        ]

    async def lock_aged_out_embedding(
        self,
        target_id: int,
        *,
        created_before: datetime,
    ) -> int | None:
        """行ロック後の新しい照会で期限切れと未完了を再確認する。"""
        locked = await self._session.scalar(
            select(AnalyzedArticleRecord.id)
            .where(AnalyzedArticleRecord.id == target_id)
            .with_for_update()
        )
        if locked is None:
            return None
        stmt = self._embedding_pending(
            select(AnalyzableArticleRecord.id),
            created_before=created_before,
        ).where(AnalyzedArticleRecord.id == target_id)
        return await self._session.scalar(stmt)

    async def count_articles_pending_curation(
        self,
        *,
        created_before: datetime,
        created_after: datetime,
    ) -> int:
        """curation/noise 未処理 article record の真の総数を返す。"""
        stmt = self._curation_pending(
            select(func.count(AnalyzableArticleRecord.id)),
            created_before=created_before,
            created_after=created_after,
        )
        result = await self._session.execute(stmt)
        return int(result.scalar_one())

    async def analyzable_article_ids_aged_out_curation(
        self,
        *,
        created_before: datetime,
        limit: int,
    ) -> list[int]:
        """``created_before`` より古い child-NULL article record ID を返す。

        curation/noise いずれの子も無く物理削除する対象。下限 (年齢ウィンドウの
        ``created_after``) を持たず、``curation_events_pending`` の
        通常再投入窓 (``[after, before)``) とは disjoint な
        「窓から落ちた古い記事」を拾う。
        """
        stmt = (
            self._curation_pending(
                select(AnalyzableArticleRecord.id), created_before=created_before
            )
            .order_by(
                AnalyzableArticleRecord.created_at.asc(),
                AnalyzableArticleRecord.id.asc(),
            )
            .limit(limit)
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def count_curations_pending_assessment(
        self,
        *,
        created_before: datetime,
        created_after: datetime,
    ) -> int:
        """assessment 未処理 curation の真の総数 (LIMIT なし COUNT)。観測専用。

        ``assessment_events_pending`` と同じ JOIN / where 条件を共有
        するが、LIMIT を持たない。Logfire gauge への observability 用途で、
        dispatch とは別経路 (dispatch は ``ASSESSMENTS_LIMIT`` で頭打ち、観測は
        それを超えた真値を出す)。

        同一 ``AsyncSession`` 内で COUNT → ID 取得を順に呼ぶことで read
        committed snapshot 上で一貫した値を返す (並行 INSERT/DELETE による
        僅かな乖離は観測値として許容)。
        """
        stmt = self._assessment_pending(
            select(func.count(ArticleCuration.id)),
            created_before=created_before,
            created_after=created_after,
        )
        result = await self._session.execute(stmt)
        return int(result.scalar_one())

    async def curation_ids_aged_out_assessment(
        self,
        *,
        created_before: datetime,
        limit: int,
    ) -> list[int]:
        """通常窓から落ちた assessment 未完了 Curation ID を返す。

        Stage 4/5 は保全価値のある部分結果を持つため物理削除せず、呼び出し側が
        ``assessment_backfill_exclusions`` に current-state sentinel を作る。
        """
        stmt = (
            self._assessment_pending(
                select(ArticleCuration.id), created_before=created_before
            )
            .order_by(
                AnalyzableArticleRecord.created_at.asc(), ArticleCuration.id.asc()
            )
            .limit(limit)
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def count_analyzed_articles_pending_embedding(
        self,
        *,
        created_before: datetime,
        created_after: datetime,
    ) -> int:
        """embedding NULL analyzed article の真の総数 (LIMIT なし COUNT)。"""
        stmt = self._embedding_pending(
            select(func.count(AnalyzedArticleRecord.id)),
            created_before=created_before,
            created_after=created_after,
        )
        result = await self._session.execute(stmt)
        return int(result.scalar_one())

    async def analyzed_article_ids_aged_out_embedding(
        self,
        *,
        created_before: datetime,
        limit: int,
    ) -> list[int]:
        """通常窓から落ちた embedding NULL AnalyzedArticleRecord ID を返す。"""
        stmt = (
            self._embedding_pending(
                select(AnalyzedArticleRecord.id), created_before=created_before
            )
            .order_by(
                AnalyzableArticleRecord.created_at.asc(), AnalyzedArticleRecord.id.asc()
            )
            .limit(limit)
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())
