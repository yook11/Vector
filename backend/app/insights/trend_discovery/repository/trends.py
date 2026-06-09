"""トレンド集計の Repository。

責務:
- ``InScopeAssessment.key_points`` JSONB の ``key_points[].mentions[]`` を 2 段
  ``jsonb_array_elements`` LATERAL で平坦化し、1 カテゴリ × 1 週分の mention を
  集計し、Trend Discovery BC の VO で返す。
- 期間境界 ``[current_start, current_end)`` (半開区間) で絞り込む。
- mention は ``COUNT(DISTINCT in_scope_assessments.id)`` で「同 assessment 内で
  同 mention が複数 key_point に登場しても 1 件」と数える (記事単位の出現を数える)。
- 名寄せは SQL 上の ``_match_key_expr`` (連続空白 collapse + trim + lower) で
  行い、display 名は ``MIN(m->>'surface')`` を採用する (casing は AI 抽出の文脈
  情報なので DB にはそのまま保存される: feedback_ai_extraction_casing.md)。
  名寄せキーは書込側 ``normalize_mention_surface`` / ``MentionName.match_key`` と
  同一規則に揃え、表記揺れによる集計の取りこぼし・count 分裂を防ぐ。
- ``type`` は Stage 4 AI 境界の ``MentionType`` (6 値 lower) を直接採用する
  (BC 境界が下流に正規化済値を保証する: feedback_bc_boundary_guarantees_downstream)。

公開クエリ:
- ``get_ranked_mentions``: floor (``appearance_count >= MIN_CURRENT``) を通過した
  mention を current/previous 件数つきで返す。ランキング確定 (出現回数 / 伸び率)
  と hot ゲートは domain の選定関数に委ねるため、並べ替えも hot ゲートもしない。
- ``get_mention_key_points``: 指定 mention 群について、現週 key_point の content を
  記事レベル dedup して最大 ``MAX_KEY_POINTS_PER_MENTION`` 本返す。
- ``get_related_mentions``: 指定 mention 群について、同一 key_point 内で一緒に
  語られた別 mention を共起記事数つきで返す。
- ``count_source_analyses``: 現週の analysis 件数 (snapshot メタ情報)。

dedup / top N の選定ポリシーは ``domain.mention_context`` の純関数へ委譲し、本
モジュールは SQL 実行と Row 詰め替え (不正 legacy・drift 行の skip + warning) まで
を持つ。

bindparam 衝突対策:
``_entity_window_subquery`` は ``get_ranked_mentions`` で current_sub と
previous_sub の 2 回呼ばれ、同じ outer query に組み込まれる。素朴な
``.bindparams(window_start=...)`` (kwarg 形式) は param 名が衝突して後者で
上書きされるため、``sa.bindparam(..., unique=True)`` を使って SQLAlchemy が
自動で suffix を付ける形にしている。
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import cast

import sqlalchemy as sa
import structlog
from pgvector.sqlalchemy import HALFVEC
from pydantic import ValidationError
from sqlalchemy import and_, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.analysis.assessment.domain.result import MentionType
from app.insights.trend_discovery.domain.mention_context import (
    KeyPointCandidate,
    select_key_points,
    select_related_mentions,
)
from app.insights.trend_discovery.domain.trend import (
    MIN_CURRENT,
    MIN_SHARED_ARTICLES,
    MentionKey,
    RankedMention,
    RelatedMention,
)
from app.models.in_scope_assessment import InScopeAssessment

logger = structlog.get_logger(__name__)

# embedding 次元 (InScopeAssessment.embedding は HALFVEC(768))。
_EMBEDDING_DIM = 768

# MentionType の既知値集合 (skip 警告の type_known 判定用)。
_VALID_MENTION_TYPES = frozenset(t.value for t in MentionType)


def _invalid_mention_log_fields(
    error: ValidationError, *, surface: object, type_: object
) -> dict[str, object]:
    """不正 mention skip 時の警告に焼く低 cardinality field (生値は出さない)。

    legacy/drift 行は type も任意文字列になりうるため、生の surface / type は出さず
    失敗 field 名・各長さ・type の既知判定のみを出す (PII / 高 cardinality を避ける)。
    """
    surface_str = surface if isinstance(surface, str) else ""
    type_str = type_ if isinstance(type_, str) else ""
    return {
        "error_fields": sorted({str(e["loc"][0]) for e in error.errors() if e["loc"]}),
        "type_known": type_str in _VALID_MENTION_TYPES,
        "type_len": len(type_str),
        "surface_len": len(surface_str),
    }


# ``_match_key_expr`` が SQL へ補間できる JSONB alias の許可リスト。SQL fragment へ
# 補間される唯一の動的部分なので、外部入力でなく固定リテラルだけを通すゲートにする。
_ALLOWED_MENTION_ALIASES = frozenset({"m", "m1", "m2"})


def _match_key_expr(alias: str) -> str:
    """mention surface の名寄せキー SQL 式を返す (連続空白 collapse + trim + lower)。

    読取側の名寄せキーを 1 式に集約し、書込側 ``normalize_mention_surface`` と
    ``MentionName.match_key`` (collapse→strip→lower) に演算順を揃える。NFKC は
    書込側で確定済みのため SQL では行わない。``alias`` は呼び出し側の固定リテラル
    のみだが、SQL fragment へ補間される唯一の動的部分なので許可リストで拒否する
    (``-O`` で剥がれる assert ではなく raise で injection 経路を構造的に封じる)。
    """
    if alias not in _ALLOWED_MENTION_ALIASES:
        msg = f"alias must be one of {sorted(_ALLOWED_MENTION_ALIASES)}, got {alias!r}"
        raise ValueError(msg)
    return (
        f"lower(btrim(regexp_replace({alias}->>'surface', '[[:space:]]+', ' ', 'g')))"
    )


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
        全 mention を返し、ランキング確定と hot ゲートは domain の選定関数
        (``select_most_mentioned`` / ``select_fastest_growing``) に委ねる。並べ替え
        もしない (どちらの軸で並べるかは選定関数の責務)。

        不正な display 名 / type の legacy・drift 行は当該 1 件のみ skip し、window
        全体を落とさない (故障は warning で可視化する)。
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
        ranked: list[RankedMention] = []
        for row in rows:
            try:
                ranked.append(
                    RankedMention(
                        name=row.display_name,
                        type=row.type,
                        appearance_count=row.appearance_count,
                        previous_appearance_count=row.previous_appearance_count,
                    )
                )
            except ValidationError as exc:
                logger.warning(
                    "trend_ranked_mention_skipped_invalid",
                    category_id=category_id,
                    **_invalid_mention_log_fields(
                        exc, surface=row.display_name, type_=row.type
                    ),
                )
        return tuple(ranked)

    async def get_mention_key_points(
        self,
        *,
        category_id: int,
        current_start: datetime,
        current_end: datetime,
        mention_keys: Sequence[MentionKey],
    ) -> dict[MentionKey, tuple[str, ...]]:
        """指定 mention 群の現週 key_point content を記事レベル dedup して返す。

        SQL は recency 降順 (analyzed_at DESC, id DESC) の候補行を取り、採択ポリシー
        (assessment 単位 dedup / embedding 近接 dedup / 最大本数) は
        ``select_key_points`` に委譲する。``mention_keys`` が空ならクエリしない。
        """
        if not mention_keys:
            return {}
        stmt = (
            # 固定リテラル補間 + bindparams のため injection 経路なし。
            # nosemgrep
            text(
                f"""
                SELECT
                  {_match_key_expr("m")} AS match_key,
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
                  AND ({_match_key_expr("m")}, m->>'type') IN :mention_keys
                -- recency 降順は select_key_points の precondition (崩すと
                -- 無言で古い key_point が採択される)。
                ORDER BY a.analyzed_at DESC, a.id DESC, e->>'content'
                """  # noqa: S608 — 補間部は _match_key_expr の固定リテラルのみ
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
        # SQL の ORDER BY (analyzed_at DESC, id DESC, content) をグループ内順序として
        # 保存する詰め替え (recency 降順は select_key_points の precondition)。
        candidates: dict[MentionKey, list[KeyPointCandidate]] = {}
        for row in rows:
            candidates.setdefault((row.match_key, row.type), []).append(
                KeyPointCandidate(
                    assessment_id=row.assessment_id,
                    embedding=_to_vector(row.embedding),
                    content=row.content,
                )
            )
        return select_key_points(candidates)

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
        ``shared_article_count >= MIN_SHARED_ARTICLES`` のみ残し、anchor ごとの
        件数降順 top N 選定は ``select_related_mentions`` に委譲する。不正な共起
        相手 (legacy・drift 行) は当該 1 件のみ skip し window 全体を落とさない
        (anchor 側は requested key なので常に正常)。``mention_keys`` が空なら
        クエリしない。
        """
        if not mention_keys:
            return {}
        stmt = (
            # 固定リテラル補間 + bindparams のため injection 経路なし。
            # nosemgrep
            text(
                f"""
                SELECT
                  {_match_key_expr("m1")} AS anchor_key,
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
                  AND ({_match_key_expr("m1")}, m1->>'type') IN :mention_keys
                  AND ({_match_key_expr("m2")}, m2->>'type')
                      <> ({_match_key_expr("m1")}, m1->>'type')
                GROUP BY
                  {_match_key_expr("m1")}, m1->>'type',
                  {_match_key_expr("m2")}, m2->>'type'
                HAVING COUNT(DISTINCT a.id) >= :min_shared
                """  # noqa: S608 — 補間部は _match_key_expr の固定リテラルのみ
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
        pairs: list[tuple[MentionKey, RelatedMention]] = []
        for row in rows:
            try:
                related = RelatedMention(
                    name=row.related_name,
                    type=row.related_type,
                    shared_article_count=row.shared_article_count,
                )
            except ValidationError as exc:
                logger.warning(
                    "trend_related_mention_skipped_invalid",
                    category_id=category_id,
                    **_invalid_mention_log_fields(
                        exc, surface=row.related_name, type_=row.related_type
                    ),
                )
                continue
            pairs.append(((row.anchor_key, row.anchor_type), related))
        return select_related_mentions(pairs)

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

        各 (match_key, type) に対して:
        - ``match_key``: ``_match_key_expr("m")`` (JOIN キー、空白 collapse +
          casing 揺れ吸収)
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
            # 固定リテラル補間 + bindparams のため injection 経路なし。
            # nosemgrep
            text(
                f"""
                SELECT
                  {_match_key_expr("m")} AS match_key,
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
                GROUP BY {_match_key_expr("m")}, m->>'type'
                """  # noqa: S608 — 補間部は _match_key_expr の固定リテラルのみ
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
        return cast("list[float]", to_list())
    return list(embedding)  # type: ignore[call-overload]
