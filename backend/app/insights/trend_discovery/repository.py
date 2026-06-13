"""トレンド集計と TrendsSnapshot 永続化の Repository。

読取側は ``analyzed_articles.key_points`` JSONB の ``key_points[].mentions[]``
を 2 段 LATERAL で平坦化して集計し、Trend Discovery BC の VO で返す。dedup /
top N の選定ポリシーは ``domain.mention_context`` の純関数へ委譲し、本モジュール
は SQL 実行と Row 詰め替え (不正 legacy・drift 行の skip + warning) までを持つ。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum
from typing import cast

import sqlalchemy as sa
import structlog
from pgvector.sqlalchemy import HALFVEC
from pydantic import ValidationError
from sqlalchemy import and_, func, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
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
from app.models.analyzed_article_record import AnalyzedArticleRecord
from app.models.trends_snapshot import TrendsSnapshot

logger = structlog.get_logger(__name__)

# embedding 次元 (AnalyzedArticleRecord.embedding は HALFVEC(768))。
_EMBEDDING_DIM = 768

# MentionType の既知値集合 (skip 警告の type_known 判定用)。
_VALID_MENTION_TYPES = frozenset(t.value for t in MentionType)


def _invalid_mention_log_fields(
    error: ValidationError, *, surface: object, type_: object
) -> dict[str, object]:
    """不正 mention skip 警告用の低 cardinality field (PII / 高 cardinality 回避の
    ため生の surface / type は出さず、失敗 field 名・長さ・type 既知判定のみ)。"""
    surface_str = surface if isinstance(surface, str) else ""
    type_str = type_ if isinstance(type_, str) else ""
    return {
        "error_fields": sorted({str(e["loc"][0]) for e in error.errors() if e["loc"]}),
        "type_known": type_str in _VALID_MENTION_TYPES,
        "type_len": len(type_str),
        "surface_len": len(surface_str),
    }


# ``_match_key_expr`` が SQL へ補間できる alias の許可リスト (補間される唯一の
# 動的部分を固定リテラルに限定する injection ゲート)。
_ALLOWED_MENTION_ALIASES = frozenset({"m", "m1", "m2"})


def _match_key_expr(alias: str) -> str:
    """mention surface の名寄せキー SQL 式を返す (連続空白 collapse + trim + lower)。

    書込側 ``normalize_mention_surface`` / ``MentionName.match_key`` と同一規則に
    揃える (NFKC は書込側で確定済み)。``alias`` は SQL fragment へ補間される唯一の
    動的部分なので、許可リスト外は raise で拒否する (``-O`` で剥がれる assert に
    しない)。
    """
    if alias not in _ALLOWED_MENTION_ALIASES:
        msg = f"alias must be one of {sorted(_ALLOWED_MENTION_ALIASES)}, got {alias!r}"
        raise ValueError(msg)
    return (
        f"lower(btrim(regexp_replace({alias}->>'surface', '[[:space:]]+', ' ', 'g')))"
    )


# ---------------------------------------------------------------------------
# TrendsRepository — トレンド集計の読取
# ---------------------------------------------------------------------------


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
        """floor (``appearance_count >= MIN_CURRENT``) を通過した全 mention を返す。

        2 ランキングは母集団が異なるため、hot ゲートも並べ替えもここでは行わず
        domain の選定関数 (``select_most_mentioned`` / ``select_fastest_growing``)
        に委ねる。不正な legacy・drift 行は当該 1 件のみ skip + warning し、
        window 全体を落とさない。
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
                  a.id AS analyzed_article_id,
                  a.analyzed_at AS analyzed_at,
                  a.embedding AS embedding,
                  e->>'content' AS content
                FROM analyzed_articles a
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
                analyzed_article_id=sa.BigInteger,
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
                    analyzed_article_id=row.analyzed_article_id,
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
        ``COUNT(DISTINCT a.id)`` (一緒に語られた記事数) で集計する。anchor ごとの
        top N 選定は ``select_related_mentions`` に委譲する。不正な共起相手は当該
        1 件のみ skip + warning し window 全体を落とさない (anchor 側は requested
        key 由来で常に正常)。``mention_keys`` が空ならクエリしない。
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
                FROM analyzed_articles a
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
        stmt = select(func.count(AnalyzedArticleRecord.id)).where(
            AnalyzedArticleRecord.analyzed_at >= current_start,
            AnalyzedArticleRecord.analyzed_at < current_end,
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

        (match_key, type) ごとに ``COUNT(DISTINCT a.id)`` で記事単位の出現数を数え
        (同 assessment 内の重複 mention は 1 件)、display 名は ``MIN(m->>'surface')``
        (casing 保持の代表値) を採用する。

        ``jsonb_typeof(...) = 'array'`` の CASE は SQL NULL / JSON null / 非配列値の
        空配列フォールバック。LATERAL は WHERE より先に評価されるため ``IS NOT
        NULL`` では遮断できず、``none_as_null=False`` の既定では Python ``None``
        が JSON null として書かれうる。bindparam の ``unique=True`` は current /
        previous 両 subquery を同一 outer query に組むときの param 名衝突回避
        (SQLAlchemy が自動 suffix)。
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
                FROM analyzed_articles a
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


# ---------------------------------------------------------------------------
# SnapshotRepository — TrendsSnapshot の永続化
# ---------------------------------------------------------------------------


class SnapshotSaveStatus(StrEnum):
    """``SnapshotRepository.save`` の永続化結果。"""

    INSERTED = "inserted"
    UPDATED = "updated"
    CONFLICT = "conflict"


@dataclass(frozen=True, slots=True)
class SnapshotSaveResult:
    """snapshot save の結果。"""

    status: SnapshotSaveStatus
    snapshot: TrendsSnapshot | None


class SnapshotRepository:
    """``trends_snapshots`` への CRUD をカプセル化する。

    snapshot は 1 集計窓分の bundle を 1 行 1 JSONB として保存する 1 単位保存が
    責務 (feedback_snapshot_responsibility.md)。
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def find_latest(self) -> TrendsSnapshot | None:
        """最新 (window_end DESC) の snapshot を 1 件返す (なければ None)。"""
        stmt = (
            select(TrendsSnapshot).order_by(TrendsSnapshot.window_end.desc()).limit(1)
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def find_by_window_end(self, window_end: date) -> TrendsSnapshot | None:
        """指定 ``window_end`` の snapshot を取得する (PK lookup)。"""
        return await self._session.get(TrendsSnapshot, window_end)

    async def exists_for_window_end(self, window_end: date) -> bool:
        """`try_advance_from` 用 cheap exists 判定 (window_end 単位)。"""
        stmt = (
            select(TrendsSnapshot.window_end)
            .where(TrendsSnapshot.window_end == window_end)
            .limit(1)
        )
        return (await self._session.execute(stmt)).first() is not None

    async def save(
        self,
        snapshot: TrendsSnapshot,
        *,
        force: bool = False,
    ) -> SnapshotSaveResult:
        """snapshot を ``trends_snapshots`` に永続化する (commit は呼び出し側の責務)。

        ``force=False`` (default) は新規 INSERT のみで、衝突時は副作用なしに
        ``CONFLICT`` (``snapshot=None``) を返す。``force=True`` は既存行を上書きし、
        ``generated_at`` も呼び出し側確定値で更新する (手動再生成経路)。
        """
        existed = False
        if force:
            existed = await self.exists_for_window_end(snapshot.window_end)
            stmt = (
                pg_insert(TrendsSnapshot)
                .values(
                    window_end=snapshot.window_end,
                    bundle=snapshot.bundle,
                    source_analysis_count=snapshot.source_analysis_count,
                    generated_at=snapshot.generated_at,
                )
                .on_conflict_do_update(
                    index_elements=["window_end"],
                    set_={
                        "bundle": snapshot.bundle,
                        "source_analysis_count": snapshot.source_analysis_count,
                        "generated_at": snapshot.generated_at,
                    },
                )
                .returning(TrendsSnapshot.window_end)
            )
        else:
            stmt = (
                pg_insert(TrendsSnapshot)
                .values(
                    window_end=snapshot.window_end,
                    bundle=snapshot.bundle,
                    source_analysis_count=snapshot.source_analysis_count,
                    generated_at=snapshot.generated_at,
                )
                .on_conflict_do_nothing(index_elements=["window_end"])
                .returning(TrendsSnapshot.window_end)
            )
        row = (await self._session.execute(stmt)).first()
        if row is None:
            return SnapshotSaveResult(
                status=SnapshotSaveStatus.CONFLICT,
                snapshot=None,
            )
        saved = TrendsSnapshot(
            window_end=row.window_end,
            bundle=snapshot.bundle,
            source_analysis_count=snapshot.source_analysis_count,
            generated_at=snapshot.generated_at,
        )
        status = (
            SnapshotSaveStatus.UPDATED
            if force and existed
            else SnapshotSaveStatus.INSERTED
        )
        return SnapshotSaveResult(status=status, snapshot=saved)
