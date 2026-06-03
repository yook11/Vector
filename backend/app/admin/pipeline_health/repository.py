"""pipeline health 観測のための read-only クエリ (Repository)。

pipeline_events / incomplete_articles を集計し、backfill 系は ``PipelineBacklog``
(= 既存 cron と同一述語) に委譲する。全クエリは観測専用で副作用を持たない。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.domain.event import EventType, Stage
from app.models.incomplete_article import IncompleteArticle
from app.models.pipeline_event import PipelineEvent
from app.queue.helpers.backlog import PipelineBacklog

# completion queue とみなす incomplete_articles の状態 (CHECK 制約と一致)。
_QUEUE_STATUSES: tuple[str, ...] = ("open", "running")


class PipelineHealthRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._backlog = PipelineBacklog(session)

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
            Stage.CURATION: await self._backlog.articles_pending_curation_stats(
                created_before=created_before, created_after=created_after
            ),
            Stage.ASSESSMENT: await self._backlog.curations_pending_assessment_stats(
                created_before=created_before, created_after=created_after
            ),
            Stage.EMBEDDING: await self._backlog.analyses_pending_embedding_stats(
                created_before=created_before, created_after=created_after
            ),
        }
