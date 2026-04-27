"""GET /api/v1/weekly-trends のレスポンス schema。

API は SSoT (Pydantic schema) → /openapi.json → frontend 型生成 の順で型を伝播
させる (CLAUDE.md)。よって snake_case domain VO を camelCase レスポンスに
明示的に詰め替える境界がここ。

設計判断:
- snapshot 不在時は 200 + 全フィールド null + 空 ``categories`` (空状態を
  ステータスコードでなく構造で表現する)。フロントは「まだ生成されていない」
  を 1 種の正常状態として扱える
- ``weekEnd`` は ``weekStart + 7 日`` を導出 (frontend が週ラベルに使う)
- ``hotnessScore`` は domain 側 ``computed_field`` の値をそのまま晒す
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from app.analysis.domain.value_objects.entity import EntityName, EntityType
from app.analysis.domain.value_objects.topic import TopicName
from app.digest.domain.trend import (
    EntityTrend,
    NewEntity,
    TopicTrend,
    WeeklyCategoryTrends,
    WeeklyTrendsBundle,
)
from app.domain.category import CategoryName, CategorySlug
from app.schemas.base import _CamelBase


class _EntityTrendOut(_CamelBase):
    name: EntityName
    type: EntityType
    current_count: int
    previous_count: int
    hotness_score: float


class _TopicTrendOut(_CamelBase):
    topic: TopicName
    current_count: int
    previous_count: int
    hotness_score: float


class _NewEntityOut(_CamelBase):
    name: EntityName
    type: EntityType
    current_count: int


class _CategoryTrendsOut(_CamelBase):
    category_id: int
    category_slug: CategorySlug
    category_name: CategoryName
    trending_entities: list[_EntityTrendOut]
    trending_topics: list[_TopicTrendOut]
    new_entities: list[_NewEntityOut]


class WeeklyTrendsResponse(_CamelBase):
    """GET /api/v1/weekly-trends のレスポンス。

    snapshot 不在時は ``empty()`` を呼び、全フィールド null + 空 categories で返す。
    """

    week_start: date | None
    week_end: date | None
    generated_at: datetime | None
    source_analysis_count: int | None
    categories: list[_CategoryTrendsOut]

    @classmethod
    def empty(cls) -> WeeklyTrendsResponse:
        return cls(
            week_start=None,
            week_end=None,
            generated_at=None,
            source_analysis_count=None,
            categories=[],
        )

    @classmethod
    def from_snapshot(
        cls,
        *,
        bundle: WeeklyTrendsBundle,
        generated_at: datetime,
        source_analysis_count: int,
    ) -> WeeklyTrendsResponse:
        return cls(
            week_start=bundle.week_start,
            week_end=bundle.week_start + timedelta(days=7),
            generated_at=generated_at,
            source_analysis_count=source_analysis_count,
            categories=[_to_category(s) for s in bundle.sections],
        )


def _to_category(section: WeeklyCategoryTrends) -> _CategoryTrendsOut:
    return _CategoryTrendsOut(
        category_id=section.category_id,
        category_slug=section.category_slug,
        category_name=section.category_name,
        trending_entities=[_to_entity(e) for e in section.trending_entities],
        trending_topics=[_to_topic(t) for t in section.trending_topics],
        new_entities=[_to_new_entity(n) for n in section.new_entities],
    )


def _to_entity(e: EntityTrend) -> _EntityTrendOut:
    return _EntityTrendOut(
        name=e.name,
        type=e.type,
        current_count=e.current_count,
        previous_count=e.previous_count,
        hotness_score=e.hotness_score,
    )


def _to_topic(t: TopicTrend) -> _TopicTrendOut:
    return _TopicTrendOut(
        topic=t.topic,
        current_count=t.current_count,
        previous_count=t.previous_count,
        hotness_score=t.hotness_score,
    )


def _to_new_entity(n: NewEntity) -> _NewEntityOut:
    return _NewEntityOut(
        name=n.name,
        type=n.type,
        current_count=n.current_count,
    )
