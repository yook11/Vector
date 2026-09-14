"""管理画面向けに工程の監査イベント・未完了件数・最古時刻を集計する。"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.domain.event import EventType, Stage
from app.models.analyzable_article_record import AnalyzableArticleRecord
from app.models.analyzed_article_record import AnalyzedArticleRecord
from app.models.article_curation import ArticleCuration
from app.models.backfill_exclusion import (
    AssessmentBackfillExclusion,
    EmbeddingBackfillExclusion,
)
from app.models.curation_noise import CurationNoise
from app.models.incomplete_article import IncompleteArticle
from app.models.out_of_scope_article_record import OutOfScopeArticleRecord
from app.models.pipeline_event import PipelineEvent

# completion queue とみなす incomplete_articles の状態 (CHECK 制約と一致)。
_QUEUE_STATUSES: tuple[str, ...] = ("open", "running")


class PipelineHealthRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def event_counts_24h(
        self, *, event_window_start: datetime
    ) -> dict[tuple[Stage, EventType], int]:
        """``(stage, event_type) -> count`` を返す (succeeded/failed のみ・24h 窓)。"""
        stmt = (
            select(
                PipelineEvent.stage,
                PipelineEvent.event_type,
                func.count(),
            )
            .where(
                PipelineEvent.occurred_at >= event_window_start,
                PipelineEvent.event_type.in_((EventType.SUCCEEDED, EventType.FAILED)),
            )
            .group_by(PipelineEvent.stage, PipelineEvent.event_type)
        )
        rows = (await self._session.execute(stmt)).all()
        return {(Stage(row[0]), EventType(row[1])): int(row[2]) for row in rows}

    async def last_succeeded_at(self) -> dict[Stage, datetime]:
        """``stage -> 最新 succeeded occurred_at`` を返す (時間下限なし)。"""
        stmt = (
            select(PipelineEvent.stage, func.max(PipelineEvent.occurred_at))
            .where(
                PipelineEvent.event_type == EventType.SUCCEEDED,
            )
            .group_by(PipelineEvent.stage)
        )
        rows = (await self._session.execute(stmt)).all()
        return {Stage(row[0]): row[1] for row in rows}

    async def completion_queue(self) -> tuple[int, datetime | None]:
        """incomplete_articles の open/running 件数と最古 ``created_at`` を返す。"""
        stmt = select(
            func.count(IncompleteArticle.id),
            func.min(IncompleteArticle.created_at),
        ).where(IncompleteArticle.status.in_(_QUEUE_STATUSES))
        row = (await self._session.execute(stmt)).one()
        return int(row[0]), row[1]

    async def backfill_stats(
        self, *, created_before: datetime, created_after: datetime
    ) -> dict[Stage, tuple[int, datetime | None]]:
        """backfill 補助メトリクスの ``(件数, 最古 created_at)`` を返す。"""
        return {
            Stage.CURATION: await self.articles_pending_curation_stats(
                created_before=created_before, created_after=created_after
            ),
            Stage.ASSESSMENT: await self.curations_pending_assessment_stats(
                created_before=created_before, created_after=created_after
            ),
            Stage.EMBEDDING: (
                await self.analyzed_articles_pending_embedding_stats(
                    created_before=created_before, created_after=created_after
                )
            ),
        }

    async def articles_pending_curation_stats(
        self, *, created_before: datetime, created_after: datetime
    ) -> tuple[int, datetime | None]:
        """未完了件数と元記事の最古作成時刻を同じ照会で返す。"""
        stmt = (
            select(
                func.count(AnalyzableArticleRecord.id),
                func.min(AnalyzableArticleRecord.created_at),
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
            .where(
                ArticleCuration.id.is_(None),
                CurationNoise.id.is_(None),
                AnalyzableArticleRecord.created_at < created_before,
                AnalyzableArticleRecord.created_at >= created_after,
            )
        )
        row = (await self._session.execute(stmt)).one()
        return int(row[0]), row[1]

    async def curations_pending_assessment_stats(
        self, *, created_before: datetime, created_after: datetime
    ) -> tuple[int, datetime | None]:
        """未完了件数と元記事の最古作成時刻を同じ照会で返す。"""
        stmt = (
            select(
                func.count(ArticleCuration.id),
                func.min(AnalyzableArticleRecord.created_at),
            )
            .select_from(ArticleCuration)
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
                AnalyzableArticleRecord.created_at >= created_after,
            )
        )
        row = (await self._session.execute(stmt)).one()
        return int(row[0]), row[1]

    async def analyzed_articles_pending_embedding_stats(
        self, *, created_before: datetime, created_after: datetime
    ) -> tuple[int, datetime | None]:
        """未完了件数と元記事の最古作成時刻を同じ照会で返す。"""
        stmt = (
            select(
                func.count(AnalyzedArticleRecord.id),
                func.min(AnalyzableArticleRecord.created_at),
            )
            .select_from(AnalyzedArticleRecord)
            .join(
                ArticleCuration, ArticleCuration.id == AnalyzedArticleRecord.curation_id
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
                AnalyzableArticleRecord.created_at >= created_after,
            )
        )
        row = (await self._session.execute(stmt)).one()
        return int(row[0]), row[1]
