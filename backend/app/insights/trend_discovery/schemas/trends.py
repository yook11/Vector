"""GET /api/v1/trends のレスポンス schema。

API は SSoT (Pydantic schema) → /openapi.json → frontend 型生成 の順で型を伝播
させる (CLAUDE.md)。よって snake_case domain VO を camelCase レスポンスに
明示的に詰め替える境界がここ。

設計判断:
- snapshot 不在 / 生成済の 2 状態を ``state`` discriminator で構造的に分離
  (``"empty"`` には窓情報フィールドが存在しない。フロントは
  ``data.state === "empty"`` で型 narrowing できる)。``state`` は処理の
  ライフサイクル語ではなく consumer が判別すべき条件 (どの resource か) を表す
- ``windowStart`` は ``windowEnd - 7 日`` を導出 (frontend が表示レンジに使う)
- ``growthRate`` は domain 側 ``computed_field`` (hotness_score) の値を晒す
  (hot ゲートの内部語彙を API contract に出さない)
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Annotated, Literal

from pydantic import Field

from app.analysis.assessment.domain.result import MentionType
from app.insights.trend_discovery.domain.mention_name import MentionName
from app.insights.trend_discovery.domain.trend import (
    CategoryTrends,
    RankedMention,
    RelatedMention,
    TrendsBundle,
)
from app.models.value_objects.category import CategoryName, CategorySlug
from app.schemas.base import _CamelBase


class _RelatedMention(_CamelBase):
    name: MentionName
    type: MentionType
    shared_article_count: int


class _RankedMention(_CamelBase):
    name: MentionName
    type: MentionType
    appearance_count: int
    previous_appearance_count: int
    growth_rate: float
    key_points: list[str]
    related_mentions: list[_RelatedMention]


class _CategoryTrends(_CamelBase):
    category_id: int
    category_slug: CategorySlug
    category_name: CategoryName
    most_mentioned: list[_RankedMention]
    fastest_growing: list[_RankedMention]


class Trends(_CamelBase):
    """snapshot 生成済の状態。"""

    state: Literal["trends"] = "trends"
    window_start: date
    window_end: date
    generated_at: datetime
    source_analysis_count: int
    category_trends: list[_CategoryTrends]


class EmptyTrends(_CamelBase):
    """snapshot 未生成の状態 (窓情報フィールドは存在しない)。"""

    state: Literal["empty"] = "empty"


TrendsResponse = Annotated[
    Trends | EmptyTrends,
    Field(discriminator="state"),
]


def empty_trends() -> EmptyTrends:
    return EmptyTrends()


def trends_from_snapshot(
    *,
    bundle: TrendsBundle,
    generated_at: datetime,
    source_analysis_count: int,
) -> Trends:
    return Trends(
        window_start=bundle.window_end - timedelta(days=7),
        window_end=bundle.window_end,
        generated_at=generated_at,
        source_analysis_count=source_analysis_count,
        category_trends=[_to_category_trends(c) for c in bundle.category_trends],
    )


def _to_category_trends(category_trends: CategoryTrends) -> _CategoryTrends:
    return _CategoryTrends(
        category_id=category_trends.category_id,
        category_slug=category_trends.category_slug,
        category_name=category_trends.category_name,
        most_mentioned=[_to_mention(m) for m in category_trends.most_mentioned],
        fastest_growing=[_to_mention(m) for m in category_trends.fastest_growing],
    )


def _to_mention(m: RankedMention) -> _RankedMention:
    return _RankedMention(
        name=m.name,
        type=m.type,
        appearance_count=m.appearance_count,
        previous_appearance_count=m.previous_appearance_count,
        growth_rate=m.hotness_score,
        key_points=list(m.key_points),
        related_mentions=[_to_related(r) for r in m.related_mentions],
    )


def _to_related(r: RelatedMention) -> _RelatedMention:
    return _RelatedMention(
        name=r.name,
        type=r.type,
        shared_article_count=r.shared_article_count,
    )
