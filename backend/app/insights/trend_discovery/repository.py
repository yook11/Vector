"""公開日時でトレンドを集計し、集計結果を保存する。"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
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
from app.insights.trend_discovery.domain.mention_context import select_co_mentions
from app.insights.trend_discovery.domain.ready import TrendDiscoveryReadyBuildFacts
from app.insights.trend_discovery.domain.trend import (
    MAX_KEY_POINTS_PER_MENTION,
    MIN_CANDIDATE_COUNT,
    MIN_SHARED_ARTICLES,
    CoMention,
    MentionCandidate,
    MentionKey,
    TrendWeeks,
    Week,
)
from app.models.analyzable_article_record import AnalyzableArticleRecord
from app.models.analyzed_article_record import AnalyzedArticleRecord
from app.models.article_curation import ArticleCuration
from app.models.category import Category
from app.models.trends_snapshot import TrendsSnapshot

logger = structlog.get_logger(__name__)

_VALID_MENTION_TYPES = frozenset(t.value for t in MentionType)


def _invalid_mention_log_fields(
    error: ValidationError, *, surface: object, type_: object
) -> dict[str, object]:
    """不正なメンションの値をログに出さず、失敗項目・長さ・種別の妥当性だけを返す。"""
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


class TrendsRepository:
    """トレンド集計に必要な事実と候補を取得する。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def load_ready_build_facts(
        self, *, weeks: TrendWeeks
    ) -> TrendDiscoveryReadyBuildFacts:
        """生成済みかを読み、未生成の場合だけ週に公開された記事数を取得する。"""
        already_generated = await SnapshotRepository(
            self._session
        ).exists_for_snapshot_date(weeks.snapshot_date)
        article_count = (
            None
            if already_generated
            else await self.count_analyzed_articles(week=weeks.week)
        )
        return TrendDiscoveryReadyBuildFacts(
            already_generated=already_generated, analyzed_article_count=article_count
        )

    async def get_categories(self) -> tuple[Category, ...]:
        stmt = select(Category).order_by(Category.id)
        return tuple((await self._session.execute(stmt)).scalars().all())

    async def get_mention_candidates(
        self, *, category_id: int, weeks: TrendWeeks
    ) -> tuple[MentionCandidate, ...]:
        """週に最低記事数以上で登場した名前を、週と前週の記事数付きで返す。"""
        week_sub = self._mention_counts_subquery(
            category_id=category_id, week=weeks.week, label="week"
        )
        previous_week_sub = self._mention_counts_subquery(
            category_id=category_id, week=weeks.previous_week, label="previous_week"
        )
        previous_week_count = func.coalesce(previous_week_sub.c.cnt, 0)
        stmt = (
            select(
                week_sub.c.display_name,
                week_sub.c.type,
                week_sub.c.cnt.label("week_count"),
                previous_week_count.label("previous_week_count"),
            )
            .select_from(week_sub)
            .outerjoin(
                previous_week_sub,
                and_(
                    previous_week_sub.c.match_key == week_sub.c.match_key,
                    previous_week_sub.c.type == week_sub.c.type,
                ),
            )
            .where(week_sub.c.cnt >= MIN_CANDIDATE_COUNT)
        )
        rows = (await self._session.execute(stmt)).all()
        candidates: list[MentionCandidate] = []
        for row in rows:
            try:
                candidates.append(
                    MentionCandidate(
                        name=row.display_name,
                        type=row.type,
                        count=row.week_count,
                        previous_week_count=row.previous_week_count,
                    )
                )
            except ValidationError as exc:
                logger.warning(
                    "trend_mention_candidate_skipped_invalid",
                    category_id=category_id,
                    **_invalid_mention_log_fields(
                        exc, surface=row.display_name, type_=row.type
                    ),
                )
        return tuple(candidates)

    async def get_mention_key_points(
        self,
        *,
        category_id: int,
        week: Week,
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
                AnalyzableArticleRecord.published_at >= week.published_from,
                AnalyzableArticleRecord.published_at < week.published_before,
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

    async def get_co_mentions(
        self,
        *,
        category_id: int,
        week: Week,
        mention_keys: Sequence[MentionKey],
    ) -> dict[MentionKey, tuple[CoMention, ...]]:
        """週に公開された記事の同じ要点で一緒に出てきた別の名前を記事数順に返す。"""
        if not mention_keys:
            return {}
        points = _json_array_elements(AnalyzedArticleRecord.key_points, "points")
        anchors = _json_array_elements(points.c.value["mentions"], "anchors")
        co_mentions = _json_array_elements(points.c.value["mentions"], "co_mentions")
        anchor_key = _match_key_expr(anchors.c.value)
        anchor_type = anchors.c.value["type"].astext
        co_mention_key = _match_key_expr(co_mentions.c.value)
        co_mention_type = co_mentions.c.value["type"].astext
        shared_count = func.count(sa.distinct(AnalyzedArticleRecord.id))
        stmt = (
            select(
                anchor_key.label("anchor_key"),
                anchor_type.label("anchor_type"),
                func.min(co_mentions.c.value["surface"].astext).label(
                    "co_mention_name"
                ),
                co_mention_type.label("co_mention_type"),
                shared_count.label("shared_article_count"),
            )
            .select_from(_published_articles())
            .join(points, sa.true())
            .join(anchors, sa.true())
            .join(co_mentions, sa.true())
            .where(
                AnalyzedArticleRecord.category_id == category_id,
                AnalyzableArticleRecord.published_at >= week.published_from,
                AnalyzableArticleRecord.published_at < week.published_before,
                sa.tuple_(anchor_key, anchor_type).in_(mention_keys),
                sa.tuple_(co_mention_key, co_mention_type)
                != sa.tuple_(anchor_key, anchor_type),
            )
            .group_by(anchor_key, anchor_type, co_mention_key, co_mention_type)
            .having(shared_count >= MIN_SHARED_ARTICLES)
        )
        rows = (await self._session.execute(stmt)).all()
        pairs: list[tuple[MentionKey, CoMention]] = []
        for row in rows:
            try:
                co_mention = CoMention(
                    name=row.co_mention_name,
                    type=row.co_mention_type,
                    shared_article_count=row.shared_article_count,
                )
            except ValidationError as exc:
                logger.warning(
                    "trend_co_mention_skipped_invalid",
                    category_id=category_id,
                    **_invalid_mention_log_fields(
                        exc, surface=row.co_mention_name, type_=row.co_mention_type
                    ),
                )
                continue
            pairs.append(((row.anchor_key, row.anchor_type), co_mention))
        return select_co_mentions(pairs)

    async def count_analyzed_articles(self, *, week: Week) -> int:
        """週に公開された分析済み記事を、要点の有無によらず数える。"""
        stmt = (
            select(func.count(AnalyzedArticleRecord.id))
            .select_from(_published_articles())
            .where(
                AnalyzableArticleRecord.published_at >= week.published_from,
                AnalyzableArticleRecord.published_at < week.published_before,
            )
        )
        return (await self._session.execute(stmt)).scalar_one()

    @staticmethod
    def _mention_counts_subquery(*, category_id: int, week: Week, label: str):
        """週に公開された記事を名前ごとに重複なく数える。"""
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
                AnalyzableArticleRecord.published_at >= week.published_from,
                AnalyzableArticleRecord.published_at < week.published_before,
            )
            .group_by(match_key, mention_type)
            .subquery(label)
        )


class SnapshotSaveStatus(StrEnum):
    """集計結果の保存状態。"""

    INSERTED = "inserted"
    CONFLICT = "conflict"


@dataclass(frozen=True, slots=True)
class SnapshotSaveResult:
    """集計結果の保存状態と保存済みデータ。"""

    status: SnapshotSaveStatus
    snapshot: TrendsSnapshot | None


class SnapshotRepository:
    """1つの週の全カテゴリ集計を1行で保存・取得する。

    スナップショットの日付は ``window_end`` 列に保存する。
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def find_latest(self) -> TrendsSnapshot | None:
        """スナップショットの日付が最も新しい集計結果を返す。"""
        stmt = (
            select(TrendsSnapshot).order_by(TrendsSnapshot.window_end.desc()).limit(1)
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def find_by_snapshot_date(self, snapshot_date: date) -> TrendsSnapshot | None:
        """指定した日付のスナップショットを返す。"""
        return await self._session.get(TrendsSnapshot, snapshot_date)

    async def exists_for_snapshot_date(self, snapshot_date: date) -> bool:
        """指定した日付のスナップショットが生成済みかを確認する。"""
        stmt = (
            select(TrendsSnapshot.window_end)
            .where(TrendsSnapshot.window_end == snapshot_date)
            .limit(1)
        )
        return (await self._session.execute(stmt)).first() is not None

    async def save(self, snapshot: TrendsSnapshot) -> SnapshotSaveResult:
        """既存の集計結果を上書きせず追加し、コミットは呼び出し側に委ねる。"""
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
