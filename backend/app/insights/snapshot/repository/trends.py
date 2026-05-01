"""週次トレンド集計の Repository。

責務:
- ``ArticleAnalysis`` / ``ArticleExtractionEntity`` を JOIN して 1 カテゴリ × 1
  週分の hot entity / hot topic / new entity を集計し、digest BC の VO で返す。
- 期間境界 ``[current_start, current_end)`` (半開区間) で絞り込む。
- entity は ``COUNT(DISTINCT extraction_id)`` で同一 extraction 内重複を排除する。
- 名寄せは SQL 上の ``lower(surface)`` / ``lower(raw_type)`` で行い、display
  名は ``MIN(surface)`` を採用する (casing は AI 抽出の文脈情報なので DB には
  そのまま保存される: feedback_ai_extraction_casing.md)。Phase 1B α-1 では旧
  ``article_entities`` (lower 正規化済み ``EntityType``) から
  ``article_extraction_entities`` (casing 保持の ``EntityRawType``) に
  schema 切替したが、α 期は集計時に lower 化することで digest 側の
  ``EntityType`` 不変条件 / 既存 UI 表示を維持する (β で canonical_type
  ベース集計に切り替わる際に casing 保持が活きる)。

並び順は Python 側で hotness_score 降順に sort する (DB 依存を避ける)。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import and_, func, or_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.insights.snapshot.config import MIN_CURRENT, MIN_PREVIOUS, NEW_BURST_THRESHOLD
from app.insights.snapshot.domain.trend import EntityTrend, NewEntity, TopicTrend
from app.models.article_analysis import ArticleAnalysis
from app.models.article_extraction_entity import ArticleExtractionEntity


class TrendsRepository:
    """週次トレンド集計のための DB アクセスをカプセル化する。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_trending_entities(
        self,
        *,
        category_id: int,
        current_start: datetime,
        current_end: datetime,
        previous_start: datetime,
    ) -> tuple[EntityTrend, ...]:
        """1 カテゴリ × 1 週分の hot entity を集計して返す。

        hot 判定: ``current >= MIN_CURRENT AND
        (previous >= MIN_PREVIOUS OR current >= NEW_BURST_THRESHOLD)``
        (continued trend / new burst の OR)。
        """
        current_sub = self._entity_window_subquery(
            category_id=category_id,
            window_start=current_start,
            window_end=current_end,
            label="current",
        )
        previous_sub = self._entity_window_subquery(
            category_id=category_id,
            window_start=previous_start,
            window_end=current_start,
            label="previous",
        )
        previous_count = func.coalesce(previous_sub.c.cnt, 0)
        stmt = (
            select(
                current_sub.c.display_name,
                current_sub.c.type,
                current_sub.c.cnt.label("current_count"),
                previous_count.label("previous_count"),
            )
            .select_from(current_sub)
            .outerjoin(
                previous_sub,
                and_(
                    previous_sub.c.match_key == current_sub.c.match_key,
                    previous_sub.c.type == current_sub.c.type,
                ),
            )
            .where(
                current_sub.c.cnt >= MIN_CURRENT,
                or_(
                    previous_count >= MIN_PREVIOUS,
                    current_sub.c.cnt >= NEW_BURST_THRESHOLD,
                ),
            )
        )
        rows = (await self._session.execute(stmt)).all()
        trends = tuple(
            EntityTrend(
                name=row.display_name,
                type=row.type,
                current_count=row.current_count,
                previous_count=row.previous_count,
            )
            for row in rows
        )
        return tuple(sorted(trends, key=lambda t: t.hotness_score, reverse=True))

    async def get_trending_topics(
        self,
        *,
        category_id: int,
        current_start: datetime,
        current_end: datetime,
        previous_start: datetime,
    ) -> tuple[TopicTrend, ...]:
        """1 カテゴリ × 1 週分の hot topic を集計して返す。

        topic は ``ArticleAnalysis`` の単一カラムなので 1 analysis = 1 件として
        ``COUNT(*)`` で数える。
        """
        current_sub = self._topic_window_subquery(
            category_id=category_id,
            window_start=current_start,
            window_end=current_end,
            label="current_topic",
        )
        previous_sub = self._topic_window_subquery(
            category_id=category_id,
            window_start=previous_start,
            window_end=current_start,
            label="previous_topic",
        )
        previous_count = func.coalesce(previous_sub.c.cnt, 0)
        stmt = (
            select(
                current_sub.c.topic,
                current_sub.c.cnt.label("current_count"),
                previous_count.label("previous_count"),
            )
            .select_from(current_sub)
            .outerjoin(
                previous_sub,
                previous_sub.c.topic == current_sub.c.topic,
            )
            .where(
                current_sub.c.cnt >= MIN_CURRENT,
                or_(
                    previous_count >= MIN_PREVIOUS,
                    current_sub.c.cnt >= NEW_BURST_THRESHOLD,
                ),
            )
        )
        rows = (await self._session.execute(stmt)).all()
        trends = tuple(
            TopicTrend(
                topic=row.topic,
                current_count=row.current_count,
                previous_count=row.previous_count,
            )
            for row in rows
        )
        return tuple(sorted(trends, key=lambda t: t.hotness_score, reverse=True))

    async def get_new_entities(
        self,
        *,
        category_id: int,
        current_start: datetime,
        current_end: datetime,
        lookback_start: datetime,
    ) -> tuple[NewEntity, ...]:
        """過去 lookback 期間に出現履歴のない初出 entity を返す。

        現週で 1 件以上出現していて、かつ ``[lookback_start, current_start)`` 区間に
        同 (lower(name), type) の出現が無い entity が new。lookback の参照は
        category 単位 (他カテゴリの出現は new 判定に影響しない)。
        """
        current_sub = self._entity_window_subquery(
            category_id=category_id,
            window_start=current_start,
            window_end=current_end,
            label="current_new",
        )
        lookback_sub = self._entity_window_subquery(
            category_id=category_id,
            window_start=lookback_start,
            window_end=current_start,
            label="lookback",
        )
        stmt = (
            select(
                current_sub.c.display_name,
                current_sub.c.type,
                current_sub.c.cnt.label("current_count"),
            )
            .select_from(current_sub)
            .outerjoin(
                lookback_sub,
                and_(
                    lookback_sub.c.match_key == current_sub.c.match_key,
                    lookback_sub.c.type == current_sub.c.type,
                ),
            )
            .where(
                current_sub.c.cnt >= 1,
                lookback_sub.c.match_key.is_(None),
            )
        )
        rows = (await self._session.execute(stmt)).all()
        new_entities = tuple(
            NewEntity(
                name=row.display_name,
                type=row.type,
                current_count=row.current_count,
            )
            for row in rows
        )
        # snapshot 側の上位 N 件 truncate が意味を持つよう
        # current_count 降順で確定する。
        return tuple(sorted(new_entities, key=lambda e: e.current_count, reverse=True))

    async def count_source_analyses(
        self, *, current_start: datetime, current_end: datetime
    ) -> int:
        """指定 window 内の analysis 件数を全カテゴリ合算で返す (snapshot メタ情報)。"""
        stmt = select(func.count(ArticleAnalysis.id)).where(
            ArticleAnalysis.analyzed_at >= current_start,
            ArticleAnalysis.analyzed_at < current_end,
        )
        return (await self._session.execute(stmt)).scalar_one()

    @staticmethod
    def _entity_window_subquery(
        *,
        category_id: int,
        window_start: datetime,
        window_end: datetime,
        label: str,
    ):
        """1 期間分の entity 集計 subquery。

        各 (lower(surface), lower(raw_type)) に対して:
        - ``match_key``: ``lower(surface)`` (JOIN キー)
        - ``type``: ``lower(raw_type)`` (digest 側 ``EntityType`` の不変条件と
          整合させるため α 期は lower 化、β で canonical_type に切替)
        - ``display_name``: ``MIN(surface)`` (display 用の casing 保持代表)
        - ``cnt``: ``COUNT(DISTINCT extraction_id)`` (同 extraction 内重複排除)
        """
        match_key = func.lower(ArticleExtractionEntity.surface)
        type_key = func.lower(ArticleExtractionEntity.raw_type)
        return (
            select(
                match_key.label("match_key"),
                type_key.label("type"),
                func.min(ArticleExtractionEntity.surface).label("display_name"),
                func.count(func.distinct(ArticleExtractionEntity.extraction_id)).label(
                    "cnt"
                ),
            )
            .join(
                ArticleAnalysis,
                ArticleAnalysis.extraction_id == ArticleExtractionEntity.extraction_id,
            )
            .where(
                ArticleAnalysis.category_id == category_id,
                ArticleAnalysis.analyzed_at >= window_start,
                ArticleAnalysis.analyzed_at < window_end,
            )
            .group_by(match_key, type_key)
            .subquery(label)
        )

    @staticmethod
    def _topic_window_subquery(
        *,
        category_id: int,
        window_start: datetime,
        window_end: datetime,
        label: str,
    ):
        """1 期間分の topic 集計 subquery (1 analysis = 1 件)。"""
        return (
            select(
                ArticleAnalysis.topic.label("topic"),
                func.count(ArticleAnalysis.id).label("cnt"),
            )
            .where(
                ArticleAnalysis.category_id == category_id,
                ArticleAnalysis.analyzed_at >= window_start,
                ArticleAnalysis.analyzed_at < window_end,
            )
            .group_by(ArticleAnalysis.topic)
            .subquery(label)
        )
