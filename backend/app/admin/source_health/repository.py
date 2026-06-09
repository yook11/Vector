"""ソース別 health 観測のための read-only クエリ (Repository)。

news_sources を駆動表に、pipeline_events / incomplete_articles を source_id 別に
集計する。pipeline_events は「この画面の指標に効く event だけ」を WHERE で絞って
取り、意味付け (analyzable/processed/failure への分類) は service が行う。全クエリは
観測専用で副作用を持たない。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import ColumnElement, and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.domain.event import EventType, Stage
from app.models.incomplete_article import IncompleteArticle
from app.models.news_source import NewsSource
from app.models.pipeline_event import PipelineEvent

# 指標対象の stage (stage1 即時保存 + stage2 補完保存)。
_TARGET_STAGES: tuple[Stage, ...] = (Stage.ACQUISITION, Stage.COMPLETION)

# analyzable と数える成功の outcome_code (stage ごとに固定)。stage×outcome の
# 入れ違い (例 completion/succeeded/article_created) や incomplete_article_created は
# この述語に合致しない。
_ACQUISITION_ANALYZABLE = "article_created"
_COMPLETION_ANALYZABLE = "article_completed"

# incomplete count とみなす incomplete_articles の状態 (CHECK 制約と一致)。
_INCOMPLETE_STATUSES: tuple[str, ...] = ("open", "running")


def _analyzable_event_clause() -> ColumnElement[bool]:
    """analyzable 記事を生んだ成功 event の述語 (両 stage の exact pair)。

    analyzable count と last succeeded at の両方が同じ「分析可能記事の成功」だけを
    対象にするための共有述語。incomplete_article_created や stage×outcome の入れ違いは
    合致しない。
    """
    return or_(
        and_(
            PipelineEvent.stage == Stage.ACQUISITION,
            PipelineEvent.event_type == EventType.SUCCEEDED,
            PipelineEvent.outcome_code == _ACQUISITION_ANALYZABLE,
        ),
        and_(
            PipelineEvent.stage == Stage.COMPLETION,
            PipelineEvent.event_type == EventType.SUCCEEDED,
            PipelineEvent.outcome_code == _COMPLETION_ANALYZABLE,
        ),
    )


class SourceHealthRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def all_sources(self) -> list[NewsSource]:
        """全 news_sources を name 昇順で返す (一覧の駆動表)。"""
        stmt = select(NewsSource).order_by(NewsSource.name, NewsSource.id)
        return list((await self._session.execute(stmt)).scalars().all())

    async def windowed_metric_event_counts(
        self, *, window_start: datetime
    ) -> list[tuple[int, str, str, int]]:
        """窓内の指標対象 event を ``(source_id, event_type, outcome_code)`` で集計。

        取得対象は analyzable 2 種 (acquisition/succeeded/article_created,
        completion/succeeded/article_completed) と、両 stage の failed / rejected。
        stage は WHERE で吸収するため返却行には含めない (failure reasons は
        outcome_code 単位で stage を跨いで合算する)。``source_id`` NULL の event
        (source 削除済み) は除外する。
        """
        stmt = (
            select(
                PipelineEvent.source_id,
                PipelineEvent.event_type,
                PipelineEvent.outcome_code,
                func.count(),
            )
            .where(
                PipelineEvent.occurred_at >= window_start,
                PipelineEvent.source_id.is_not(None),
                or_(
                    _analyzable_event_clause(),
                    and_(
                        PipelineEvent.stage.in_(_TARGET_STAGES),
                        PipelineEvent.event_type.in_(
                            (EventType.FAILED, EventType.REJECTED)
                        ),
                    ),
                ),
            )
            .group_by(
                PipelineEvent.source_id,
                PipelineEvent.event_type,
                PipelineEvent.outcome_code,
            )
        )
        rows = (await self._session.execute(stmt)).all()
        return [(int(r[0]), r[1], r[2], int(r[3])) for r in rows]

    async def incomplete_counts(self) -> dict[int, int]:
        """``source_id -> open/running の incomplete 件数`` を返す (窓非依存)。"""
        stmt = (
            select(IncompleteArticle.source_id, func.count())
            .where(IncompleteArticle.status.in_(_INCOMPLETE_STATUSES))
            .group_by(IncompleteArticle.source_id)
        )
        rows = (await self._session.execute(stmt)).all()
        return {int(r[0]): int(r[1]) for r in rows}

    async def last_succeeded_at(self) -> dict[int, datetime]:
        """``source_id -> 最新の analyzable 成功 occurred_at`` を返す。

        analyzable count と同じ「分析可能記事を生んだ成功」(article_created /
        article_completed) だけを対象にし、incomplete_article_created で
        「直近成功」が偽って新しく見えるのを防ぐ。表示窓に依存しない。
        """
        stmt = (
            select(PipelineEvent.source_id, func.max(PipelineEvent.occurred_at))
            .where(
                _analyzable_event_clause(),
                PipelineEvent.source_id.is_not(None),
            )
            .group_by(PipelineEvent.source_id)
        )
        rows = (await self._session.execute(stmt)).all()
        return {int(r[0]): r[1] for r in rows}
