"""旧 Taskiq backfill の対象取得と件数観測を担う。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import Select, func, select, true
from sqlalchemy.ext.asyncio import AsyncSession

from app.backfill.targets import BackfillTarget
from app.collection.sources.source_name import SourceName
from app.models.analyzable_article_record import AnalyzableArticleRecord
from app.models.analyzed_article_record import AnalyzedArticleRecord
from app.models.article_curation import ArticleCuration
from app.models.backfill_exclusion import (
    AssessmentBackfillExclusion,
)
from app.models.curation_noise import CurationNoise
from app.models.news_source import NewsSource
from app.models.out_of_scope_article_record import OutOfScopeArticleRecord


class PipelineBacklog:
    """旧経路の再投入対象を年齢と工程の完了状態から取得する。"""

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

    async def analyzable_article_ids_pending_curation(
        self,
        *,
        created_before: datetime,
        created_after: datetime,
        limit: int,
    ) -> list[int]:
        """curation/noise いずれの子も無い未処理 article record ID を返す。

        ``curation_noises`` (noise 判定済み = 正常完了) も anti-join する。
        signal/noise は排他で、どちらかが在れば curation は完了している
        (``ReadyForCuration.try_advance_from`` の precondition と同一定義)。
        """
        stmt = (
            self._curation_pending(
                select(AnalyzableArticleRecord.id),
                created_before=created_before,
                created_after=created_after,
            )
            .order_by(AnalyzableArticleRecord.created_at.asc())
            .limit(limit)
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def curation_targets_pending(
        self,
        *,
        created_before: datetime,
        created_after: datetime,
        limit: int,
    ) -> list[BackfillTarget]:
        """Stage 3 backfill の enqueue / audit 対象を返す。"""
        stmt = (
            select(
                AnalyzableArticleRecord.id, AnalyzableArticleRecord.id, NewsSource.name
            )
            .outerjoin(NewsSource, NewsSource.id == AnalyzableArticleRecord.source_id)
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
                AnalyzableArticleRecord.created_at >= created_after,
            )
            .order_by(AnalyzableArticleRecord.created_at.asc())
            .limit(limit)
        )
        rows = (await self._session.execute(stmt)).tuples().all()
        return [_target_from_row(row) for row in rows]

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

    async def assessment_targets_pending(
        self,
        *,
        created_before: datetime,
        created_after: datetime,
        limit: int,
    ) -> list[BackfillTarget]:
        """Stage 4 backfill の enqueue / audit 対象を返す。"""
        stmt = (
            select(
                ArticleCuration.id,
                ArticleCuration.analyzable_article_id,
                NewsSource.name,
            )
            .join(
                AnalyzableArticleRecord,
                AnalyzableArticleRecord.id == ArticleCuration.analyzable_article_id,
            )
            .outerjoin(NewsSource, NewsSource.id == AnalyzableArticleRecord.source_id)
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
                AnalyzableArticleRecord.created_at >= created_after,
            )
            .order_by(AnalyzableArticleRecord.created_at.asc())
            .limit(limit)
        )
        rows = (await self._session.execute(stmt)).tuples().all()
        return [_target_from_row(row) for row in rows]

    async def curation_ids_pending_assessment(
        self,
        *,
        created_before: datetime,
        created_after: datetime,
        limit: int,
    ) -> list[int]:
        """curation はあるが analysis / rejection が無い Curation ID を返す。"""
        stmt = (
            self._assessment_pending(
                select(ArticleCuration.id),
                created_before=created_before,
                created_after=created_after,
            )
            .order_by(AnalyzableArticleRecord.created_at.asc())
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

        ``curation_ids_pending_assessment`` と同じ JOIN / where 条件を共有
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


def _target_from_row(row: tuple[int, int, SourceName | None]) -> BackfillTarget:
    target_id, analyzable_article_id, source_name = row
    return BackfillTarget(
        target_id=target_id,
        analyzable_article_id=analyzable_article_id,
        source_name=source_name,
    )
