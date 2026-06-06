"""週次トレンド集計の Repository。

責務:
- ``InScopeAssessment.key_points`` JSONB の ``key_points[].mentions[]`` を 2 段
  ``jsonb_array_elements`` LATERAL で平坦化し、1 カテゴリ × 1 週分の hot mention /
  new mention を集計し、Trend Discovery BC の VO で返す。
- 期間境界 ``[current_start, current_end)`` (半開区間) で絞り込む。
- mention は ``COUNT(DISTINCT in_scope_assessments.id)`` で「同 assessment 内で
  同 mention が複数 key_point に登場しても 1 件」と数える (記事単位の出現を数える)。
- 名寄せは SQL 上の ``lower(m->>'surface')`` で行い、display 名は
  ``MIN(m->>'surface')`` を採用する (casing は AI 抽出の文脈情報なので
  DB にはそのまま保存される: feedback_ai_extraction_casing.md)。
- ``type`` は Stage 4 AI 境界の ``MentionType`` (6 値 lower) を直接採用する
  (BC 境界が下流に正規化済値を保証する: feedback_bc_boundary_guarantees_downstream)。

並び順は Python 側で hotness_score 降順に sort する (DB 依存を避ける)。

bindparam 衝突対策:
``_entity_window_subquery`` は ``get_trending_entities`` で current_sub と
previous_sub の 2 回呼ばれ、同じ outer query に組み込まれる。素朴な
``.bindparams(window_start=...)`` (kwarg 形式) は param 名が衝突して後者で
上書きされるため、``sa.bindparam(..., unique=True)`` を使って SQLAlchemy が
自動で suffix を付ける形にしている。
"""

from __future__ import annotations

from datetime import datetime

import sqlalchemy as sa
from sqlalchemy import and_, func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.insights.trend_discovery.config import (
    MIN_CURRENT,
    MIN_PREVIOUS,
    NEW_BURST_THRESHOLD,
)
from app.insights.trend_discovery.domain.trend import EntityTrend, NewEntity
from app.models.in_scope_assessment import InScopeAssessment


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
        """1 カテゴリ × 1 週分の hot mention を集計して返す。

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

    async def get_new_entities(
        self,
        *,
        category_id: int,
        current_start: datetime,
        current_end: datetime,
        lookback_start: datetime,
    ) -> tuple[NewEntity, ...]:
        """過去 lookback 期間に出現履歴のない初出 mention を返す。

        現週で 1 件以上出現していて、かつ ``[lookback_start, current_start)`` 区間に
        同 (lower(surface), type) の出現が無い mention が new。lookback の参照は
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
        stmt = select(func.count(InScopeAssessment.id)).where(
            InScopeAssessment.analyzed_at >= current_start,
            InScopeAssessment.analyzed_at < current_end,
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
        """1 期間分の mention 集計 subquery (key_points JSONB 2 段 LATERAL 平坦化)。

        各 (lower(surface), type) に対して:
        - ``match_key``: ``lower(m->>'surface')`` (JOIN キー、casing 揺れ吸収)
        - ``type``: ``m->>'type'`` (Stage 4 AI 境界の MentionType 6 値 lower を直接採用)
        - ``display_name``: ``MIN(m->>'surface')`` (display 用の casing 保持代表)
        - ``cnt``: ``COUNT(DISTINCT a.id)`` (同 assessment 内重複排除)

        ``jsonb_typeof(...) = 'array'`` の CASE で SQL NULL / JSON null / 非配列値を
        すべて空配列にフォールバックさせる。LATERAL は WHERE より先に評価されるため
        ``WHERE key_points IS NOT NULL`` では遮断できず、また SQLAlchemy ``JSONB`` の
        既定 (``none_as_null=False``) では Python ``None`` が JSON null として
        書かれることがあり、``jsonb_array_elements`` がスカラーで落ちる。
        ``key_points = []`` (空配列) は LATERAL が自然に 0 行返すため集計に影響しない。

        bindparam は ``unique=True`` で current / previous 両 subquery を outer query
        に入れた時に param 名が衝突しないようにする (SQLAlchemy が自動 suffix する)。
        """
        return (
            text(
                """
                SELECT
                  lower(m->>'surface') AS match_key,
                  m->>'type' AS type,
                  MIN(m->>'surface') AS display_name,
                  COUNT(DISTINCT a.id) AS cnt
                FROM in_scope_assessments a
                CROSS JOIN LATERAL jsonb_array_elements(
                  CASE WHEN jsonb_typeof(a.key_points) = 'array'
                       THEN a.key_points ELSE '[]'::jsonb END
                ) AS e
                CROSS JOIN LATERAL jsonb_array_elements(
                  CASE WHEN jsonb_typeof(e->'mentions') = 'array'
                       THEN e->'mentions' ELSE '[]'::jsonb END
                ) AS m
                WHERE a.category_id = :category_id
                  AND a.analyzed_at >= :window_start
                  AND a.analyzed_at < :window_end
                GROUP BY lower(m->>'surface'), m->>'type'
                """
            )
            .bindparams(
                sa.bindparam("category_id", category_id, unique=True),
                sa.bindparam("window_start", window_start, unique=True),
                sa.bindparam("window_end", window_end, unique=True),
            )
            .columns(
                match_key=sa.String,
                type=sa.String,
                display_name=sa.String,
                cnt=sa.BigInteger,
            )
            .subquery(label)
        )
