"""トレンド集計の Repository。

責務:
- ``InScopeAssessment.key_points`` JSONB の ``key_points[].mentions[]`` を 2 段
  ``jsonb_array_elements`` LATERAL で平坦化し、1 カテゴリ × 1 週分の mention を
  集計し、Trend Discovery BC の VO で返す。
- 期間境界 ``[current_start, current_end)`` (半開区間) で絞り込む。
- mention は ``COUNT(DISTINCT in_scope_assessments.id)`` で「同 assessment 内で
  同 mention が複数 key_point に登場しても 1 件」と数える (記事単位の出現を数える)。
- 名寄せは SQL 上の ``lower(m->>'surface')`` で行い、display 名は
  ``MIN(m->>'surface')`` を採用する (casing は AI 抽出の文脈情報なので
  DB にはそのまま保存される: feedback_ai_extraction_casing.md)。
- ``type`` は Stage 4 AI 境界の ``MentionType`` (6 値 lower) を直接採用する
  (BC 境界が下流に正規化済値を保証する: feedback_bc_boundary_guarantees_downstream)。

公開クエリ:
- ``get_ranked_mentions``: floor (``appearance_count >= MIN_CURRENT``) を通過した
  mention を current/previous 件数つきで返す。ランキング確定 (出現回数 / 伸び率)
  と hot ゲートは service が行うため、ここでは並べ替えも hot ゲートもしない。
- ``get_mention_key_points``: 指定 mention 群について、現週 key_point の content を
  記事レベル dedup して最大 ``MAX_KEY_POINTS_PER_MENTION`` 本返す。
- ``get_related_mentions``: 指定 mention 群について、同一 key_point 内で一緒に
  語られた別 mention を共起記事数つきで返す。
- ``count_source_analyses``: 現週の analysis 件数 (snapshot メタ情報)。

bindparam 衝突対策:
``_entity_window_subquery`` は ``get_ranked_mentions`` で current_sub と
previous_sub の 2 回呼ばれ、同じ outer query に組み込まれる。素朴な
``.bindparams(window_start=...)`` (kwarg 形式) は param 名が衝突して後者で
上書きされるため、``sa.bindparam(..., unique=True)`` を使って SQLAlchemy が
自動で suffix を付ける形にしている。
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from datetime import datetime

import sqlalchemy as sa
from pgvector.sqlalchemy import HALFVEC
from sqlalchemy import Row, and_, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.insights.trend_discovery.domain.trend import (
    KEY_POINT_DEDUP_DISTANCE,
    MAX_KEY_POINTS_PER_MENTION,
    MAX_RELATED_MENTIONS,
    MIN_CURRENT,
    MIN_SHARED_ARTICLES,
    RankedMention,
    RelatedMention,
)
from app.models.in_scope_assessment import InScopeAssessment

# embedding 次元 (InScopeAssessment.embedding は HALFVEC(768))。
_EMBEDDING_DIM = 768

MentionKey = tuple[str, str]


class TrendsRepository:
    """週次トレンド集計のための DB アクセスをカプセル化する。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_ranked_mentions(
        self,
        *,
        category_id: int,
        current_start: datetime,
        current_end: datetime,
        previous_start: datetime,
    ) -> tuple[RankedMention, ...]:
        """floor (``appearance_count >= MIN_CURRENT``) を通過した mention を返す。

        出現回数ランキングと伸び率ランキングは母集団が異なる (出現回数は floor の
        み・伸び率は floor + hot ゲート) ため、ここでは hot ゲートを掛けず floor の
        全 mention を返し、ランキング確定と hot ゲートは service に委ねる。並べ替え
        もしない (どちらの軸で並べるかは service の責務)。
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
        previous_appearance = func.coalesce(previous_sub.c.cnt, 0)
        stmt = (
            select(
                current_sub.c.display_name,
                current_sub.c.type,
                current_sub.c.cnt.label("appearance_count"),
                previous_appearance.label("previous_appearance_count"),
            )
            .select_from(current_sub)
            .outerjoin(
                previous_sub,
                and_(
                    previous_sub.c.match_key == current_sub.c.match_key,
                    previous_sub.c.type == current_sub.c.type,
                ),
            )
            .where(current_sub.c.cnt >= MIN_CURRENT)
        )
        rows = (await self._session.execute(stmt)).all()
        return tuple(
            RankedMention(
                name=row.display_name,
                type=row.type,
                appearance_count=row.appearance_count,
                previous_appearance_count=row.previous_appearance_count,
            )
            for row in rows
        )

    async def get_mention_key_points(
        self,
        *,
        category_id: int,
        current_start: datetime,
        current_end: datetime,
        mention_keys: Sequence[MentionKey],
    ) -> dict[MentionKey, tuple[str, ...]]:
        """指定 mention 群の現週 key_point content を記事レベル dedup して返す。

        各 mention について最新優先で走査し、同一記事 (assessment) からは 1 本まで、
        さらに embedding が ``KEY_POINT_DEDUP_DISTANCE`` 未満に近い別記事も同一トピ
        ックとして畳む。embedding が NULL の旧行は近接判定をスキップ (常に別記事扱
        い) だが、assessment 単位の dedup は効くため 1 記事 1 本は保たれる。最大
        ``MAX_KEY_POINTS_PER_MENTION`` 本。``mention_keys`` が空ならクエリしない。
        """
        if not mention_keys:
            return {}
        stmt = (
            text(
                """
                SELECT
                  lower(m->>'surface') AS match_key,
                  m->>'type' AS type,
                  a.id AS assessment_id,
                  a.analyzed_at AS analyzed_at,
                  a.embedding AS embedding,
                  e->>'content' AS content
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
                  AND a.analyzed_at >= :current_start
                  AND a.analyzed_at < :current_end
                  AND (lower(m->>'surface'), m->>'type') IN :mention_keys
                ORDER BY a.analyzed_at DESC, a.id DESC, e->>'content'
                """
            )
            .bindparams(
                sa.bindparam("category_id", category_id),
                sa.bindparam("current_start", current_start),
                sa.bindparam("current_end", current_end),
                sa.bindparam("mention_keys", value=list(mention_keys), expanding=True),
            )
            .columns(
                match_key=sa.String,
                type=sa.String,
                assessment_id=sa.BigInteger,
                analyzed_at=sa.DateTime(timezone=True),
                embedding=HALFVEC(_EMBEDDING_DIM),
                content=sa.String,
            )
        )
        rows = (await self._session.execute(stmt)).all()
        return self._dedup_key_points(rows)

    async def get_related_mentions(
        self,
        *,
        category_id: int,
        current_start: datetime,
        current_end: datetime,
        mention_keys: Sequence[MentionKey],
    ) -> dict[MentionKey, tuple[RelatedMention, ...]]:
        """指定 mention 群と同一 key_point 内で一緒に語られた別 mention を返す。

        同一 key_point 内の mention を 2 回 LATERAL 展開し (m1=anchor / m2=相手)、
        自己ペアを除き ``COUNT(DISTINCT a.id)`` (一緒に語られた記事数) で集計する。
        ``shared_article_count >= MIN_SHARED_ARTICLES`` のみ残し、anchor ごとに件数
        降順 top ``MAX_RELATED_MENTIONS``。``mention_keys`` が空ならクエリしない。
        """
        if not mention_keys:
            return {}
        stmt = (
            text(
                """
                SELECT
                  lower(m1->>'surface') AS anchor_key,
                  m1->>'type' AS anchor_type,
                  MIN(m2->>'surface') AS related_name,
                  m2->>'type' AS related_type,
                  COUNT(DISTINCT a.id) AS shared_article_count
                FROM in_scope_assessments a
                CROSS JOIN LATERAL jsonb_array_elements(
                  CASE WHEN jsonb_typeof(a.key_points) = 'array'
                       THEN a.key_points ELSE '[]'::jsonb END
                ) AS e
                CROSS JOIN LATERAL jsonb_array_elements(
                  CASE WHEN jsonb_typeof(e->'mentions') = 'array'
                       THEN e->'mentions' ELSE '[]'::jsonb END
                ) AS m1
                CROSS JOIN LATERAL jsonb_array_elements(
                  CASE WHEN jsonb_typeof(e->'mentions') = 'array'
                       THEN e->'mentions' ELSE '[]'::jsonb END
                ) AS m2
                WHERE a.category_id = :category_id
                  AND a.analyzed_at >= :current_start
                  AND a.analyzed_at < :current_end
                  AND (lower(m1->>'surface'), m1->>'type') IN :mention_keys
                  AND (lower(m2->>'surface'), m2->>'type')
                      <> (lower(m1->>'surface'), m1->>'type')
                GROUP BY
                  lower(m1->>'surface'), m1->>'type',
                  lower(m2->>'surface'), m2->>'type'
                HAVING COUNT(DISTINCT a.id) >= :min_shared
                """
            )
            .bindparams(
                sa.bindparam("category_id", category_id),
                sa.bindparam("current_start", current_start),
                sa.bindparam("current_end", current_end),
                sa.bindparam("min_shared", MIN_SHARED_ARTICLES),
                sa.bindparam("mention_keys", value=list(mention_keys), expanding=True),
            )
            .columns(
                anchor_key=sa.String,
                anchor_type=sa.String,
                related_name=sa.String,
                related_type=sa.String,
                shared_article_count=sa.BigInteger,
            )
        )
        rows = (await self._session.execute(stmt)).all()
        return self._group_related(rows)

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
    def _dedup_key_points(
        rows: Sequence[Row[tuple]],
    ) -> dict[MentionKey, tuple[str, ...]]:
        """recency 順の行を mention ごとに記事レベル dedup して content tuple 化。"""
        grouped: dict[MentionKey, list[Row[tuple]]] = {}
        for row in rows:
            grouped.setdefault((row.match_key, row.type), []).append(row)

        result: dict[MentionKey, tuple[str, ...]] = {}
        for key, group in grouped.items():
            contents: list[str] = []
            seen_assessments: set[int] = set()
            accepted_vectors: list[list[float]] = []
            for row in group:
                if len(contents) >= MAX_KEY_POINTS_PER_MENTION:
                    break
                if row.content is None or row.assessment_id in seen_assessments:
                    continue
                vector = _to_vector(row.embedding)
                if vector is not None and any(
                    _cosine_distance(vector, v) < KEY_POINT_DEDUP_DISTANCE
                    for v in accepted_vectors
                ):
                    continue
                contents.append(row.content)
                seen_assessments.add(row.assessment_id)
                if vector is not None:
                    accepted_vectors.append(vector)
            result[key] = tuple(contents)
        return result

    @staticmethod
    def _group_related(
        rows: Sequence[Row[tuple]],
    ) -> dict[MentionKey, tuple[RelatedMention, ...]]:
        """anchor ごとに共起記事数降順 top N の RelatedMention を組み立てる。"""
        grouped: dict[MentionKey, list[RelatedMention]] = {}
        for row in rows:
            anchor = (row.anchor_key, row.anchor_type)
            grouped.setdefault(anchor, []).append(
                RelatedMention(
                    name=row.related_name,
                    type=row.related_type,
                    shared_article_count=row.shared_article_count,
                )
            )
        return {
            anchor: tuple(
                sorted(
                    items,
                    key=lambda r: (-r.shared_article_count, r.name.match_key),
                )[:MAX_RELATED_MENTIONS]
            )
            for anchor, items in grouped.items()
        }

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


def _to_vector(embedding: object) -> list[float] | None:
    """HALFVEC 列の読み出し結果を float list に正規化する (NULL は None)。"""
    if embedding is None:
        return None
    to_list = getattr(embedding, "to_list", None)
    if callable(to_list):
        return to_list()
    return list(embedding)  # type: ignore[call-overload]


def _cosine_distance(a: list[float], b: list[float]) -> float:
    """cosine 距離 (1 - cosine 類似度)。ゼロベクトルは最大距離扱い。"""
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 1.0
    return 1.0 - dot / (norm_a * norm_b)
