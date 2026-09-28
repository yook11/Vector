"""公開日時でトレンドを集計し、記事ごとの要点を選定してsnapshotを保存する。"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum
from typing import Any

import sqlalchemy as sa
import structlog
from pydantic import ValidationError
from sqlalchemy import and_, func, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement
from sqlalchemy.sql.selectable import Join, TableValuedAlias

from app.analysis.assessment.domain.result import MentionType
from app.insights.trend_discovery.domain.mention_context import (
    select_related_mentions,
)
from app.insights.trend_discovery.domain.trend import (
    MAX_KEY_POINTS_PER_MENTION,
    MIN_CURRENT,
    MIN_SHARED_ARTICLES,
    MentionKey,
    RankedMention,
    RelatedMention,
)
from app.models.analyzable_article_record import AnalyzableArticleRecord
from app.models.analyzed_article_record import AnalyzedArticleRecord
from app.models.article_curation import ArticleCuration
from app.models.trends_snapshot import TrendsSnapshot

logger = structlog.get_logger(__name__)

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


def _match_key_expr(mention: ColumnElement[Any]) -> ColumnElement[str]:
    """書込側と同じ空白の正規化と小文字化でメンションを照合する。"""
    return func.lower(
        func.btrim(
            func.regexp_replace(mention["surface"].astext, "[[:space:]]+", " ", "g")
        )
    )


def _json_array_elements(
    value: ColumnElement[Any], name: str, *, with_ordinality: str | None = None
) -> TableValuedAlias:
    """旧データのNULL・非配列値を空配列として扱う。"""
    return (
        func.jsonb_array_elements(
            sa.case(
                (func.jsonb_typeof(value) == "array", value),
                else_=func.jsonb_build_array(),
            )
        )
        .table_valued(sa.column("value", JSONB), with_ordinality=with_ordinality)
        .render_derived(name=name)
        .lateral()
    )


def _published_articles() -> Join:
    return AnalyzedArticleRecord.__table__.join(
        ArticleCuration, AnalyzedArticleRecord.curation_id == ArticleCuration.id
    ).join(
        AnalyzableArticleRecord,
        ArticleCuration.analyzable_article_id == AnalyzableArticleRecord.id,
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
        """公開日時の新しい最大3記事から、そのメンションの最初の有効な要点を返す。"""
        if not mention_keys:
            return {}
        points = _json_array_elements(
            AnalyzedArticleRecord.key_points, "points", with_ordinality="position"
        )
        mentions = _json_array_elements(points.c.value["mentions"], "mentions")
        match_key = _match_key_expr(mentions.c.value)
        mention_type = mentions.c.value["type"].astext
        content = points.c.value["content"].astext
        candidates = (
            select(
                match_key.label("match_key"),
                mention_type.label("type"),
                AnalyzedArticleRecord.id.label("article_id"),
                AnalyzableArticleRecord.published_at,
                content.label("content"),
                func.row_number()
                .over(
                    partition_by=(match_key, mention_type, AnalyzedArticleRecord.id),
                    order_by=points.c.position,
                )
                .label("point_rank"),
            )
            .select_from(_published_articles())
            .join(points, sa.true())
            .join(mentions, sa.true())
            .where(
                AnalyzedArticleRecord.category_id == category_id,
                AnalyzableArticleRecord.published_at >= current_start,
                AnalyzableArticleRecord.published_at < current_end,
                sa.tuple_(match_key, mention_type).in_(mention_keys),
                func.jsonb_typeof(points.c.value["content"]) == "string",
                content.op("~")("[^[:space:]]"),
            )
            .subquery("article_points")
        )
        articles = (
            select(
                candidates.c.match_key,
                candidates.c.type,
                candidates.c.content,
                func.row_number()
                .over(
                    partition_by=(candidates.c.match_key, candidates.c.type),
                    order_by=(
                        candidates.c.published_at.desc(),
                        candidates.c.article_id.desc(),
                    ),
                )
                .label("article_rank"),
            )
            .where(candidates.c.point_rank == 1)
            .subquery("latest_article_points")
        )
        stmt = (
            select(articles.c.match_key, articles.c.type, articles.c.content)
            .where(articles.c.article_rank <= MAX_KEY_POINTS_PER_MENTION)
            .order_by(articles.c.match_key, articles.c.type, articles.c.article_rank)
        )
        rows = (await self._session.execute(stmt)).all()
        contents: dict[MentionKey, list[str]] = {}
        for row in rows:
            contents.setdefault((row.match_key, row.type), []).append(row.content)
        return {key: tuple(values) for key, values in contents.items()}

    async def get_related_mentions(
        self,
        *,
        category_id: int,
        current_start: datetime,
        current_end: datetime,
        mention_keys: Sequence[MentionKey],
    ) -> dict[MentionKey, tuple[RelatedMention, ...]]:
        """公開期間内の同じ要点で共起した別メンションを記事数順に返す。"""
        if not mention_keys:
            return {}
        points = _json_array_elements(AnalyzedArticleRecord.key_points, "points")
        anchors = _json_array_elements(points.c.value["mentions"], "anchors")
        related_mentions = _json_array_elements(points.c.value["mentions"], "related")
        anchor_key = _match_key_expr(anchors.c.value)
        anchor_type = anchors.c.value["type"].astext
        related_key = _match_key_expr(related_mentions.c.value)
        related_type = related_mentions.c.value["type"].astext
        shared_count = func.count(sa.distinct(AnalyzedArticleRecord.id))
        stmt = (
            select(
                anchor_key.label("anchor_key"),
                anchor_type.label("anchor_type"),
                func.min(related_mentions.c.value["surface"].astext).label(
                    "related_name"
                ),
                related_type.label("related_type"),
                shared_count.label("shared_article_count"),
            )
            .select_from(_published_articles())
            .join(points, sa.true())
            .join(anchors, sa.true())
            .join(related_mentions, sa.true())
            .where(
                AnalyzedArticleRecord.category_id == category_id,
                AnalyzableArticleRecord.published_at >= current_start,
                AnalyzableArticleRecord.published_at < current_end,
                sa.tuple_(anchor_key, anchor_type).in_(mention_keys),
                sa.tuple_(related_key, related_type)
                != sa.tuple_(anchor_key, anchor_type),
            )
            .group_by(anchor_key, anchor_type, related_key, related_type)
            .having(shared_count >= MIN_SHARED_ARTICLES)
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
        """期間内に公開された分析済み記事を、要点の有無によらず数える。"""
        stmt = (
            select(func.count(AnalyzedArticleRecord.id))
            .select_from(_published_articles())
            .where(
                AnalyzableArticleRecord.published_at >= current_start,
                AnalyzableArticleRecord.published_at < current_end,
            )
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
        """期間内に公開された記事をメンションごとに重複なく数える。"""
        points = _json_array_elements(AnalyzedArticleRecord.key_points, "points")
        mentions = _json_array_elements(points.c.value["mentions"], "mentions")
        match_key = _match_key_expr(mentions.c.value)
        mention_type = mentions.c.value["type"].astext
        return (
            select(
                match_key.label("match_key"),
                mention_type.label("type"),
                func.min(mentions.c.value["surface"].astext).label("display_name"),
                func.count(sa.distinct(AnalyzedArticleRecord.id)).label("cnt"),
            )
            .select_from(_published_articles())
            .join(points, sa.true())
            .join(mentions, sa.true())
            .where(
                AnalyzedArticleRecord.category_id == category_id,
                AnalyzableArticleRecord.published_at >= window_start,
                AnalyzableArticleRecord.published_at < window_end,
            )
            .group_by(match_key, mention_type)
            .subquery(label)
        )


# ---------------------------------------------------------------------------
# SnapshotRepository — TrendsSnapshot の永続化
# ---------------------------------------------------------------------------


class SnapshotSaveStatus(StrEnum):
    """``SnapshotRepository.save`` の永続化結果。"""

    INSERTED = "inserted"
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

    async def save(self, snapshot: TrendsSnapshot) -> SnapshotSaveResult:
        """snapshot を ``trends_snapshots`` に永続化する (commit は呼び出し側の責務)。

        新規 INSERT のみで、衝突時は副作用なしに ``CONFLICT`` (``snapshot=None``)
        を返す。
        """
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
        return SnapshotSaveResult(status=SnapshotSaveStatus.INSERTED, snapshot=saved)
