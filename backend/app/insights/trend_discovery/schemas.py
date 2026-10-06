"""トレンドのAPI応答形式と、集計結果からの変換を定義する。"""

from __future__ import annotations

from datetime import date, datetime

from app.analysis.assessment.domain.result import MentionType
from app.insights.trend_discovery.domain.mention_name import MentionName
from app.insights.trend_discovery.domain.trend import (
    CategoryTrends,
    CoMention,
    MentionTrend,
    TrendsBundle,
    Week,
)
from app.schemas.base import _CamelBase
from app.schemas.category import Category


class _DateRange(_CamelBase):
    start: date
    # 最終日 (含む)。
    end: date


class _CoMention(_CamelBase):
    name: MentionName
    type: MentionType
    shared_article_count: int


class _MentionArticleVolume(_CamelBase):
    count: int
    previous_week_count: int
    rank: int


class _MentionGrowth(_CamelBase):
    rate: float
    rank: int | None


class _MentionTrend(_CamelBase):
    name: MentionName
    type: MentionType
    article_volume: _MentionArticleVolume
    growth: _MentionGrowth
    key_points: list[str]
    mentioned_with: list[_CoMention]


class _CategoryTrends(_CamelBase):
    category: Category
    mention_trends: list[_MentionTrend]


class Trends(_CamelBase):
    """1つの週のトレンド (保存したスナップショットの内容)。"""

    week: _DateRange
    previous_week: _DateRange
    generated_at: datetime
    analyzed_article_count: int
    category_trends: list[_CategoryTrends]


def trends_from_snapshot(
    *,
    bundle: TrendsBundle,
    generated_at: datetime,
    analyzed_article_count: int,
) -> Trends:
    return Trends(
        week=_to_date_range(bundle.weeks.week),
        previous_week=_to_date_range(bundle.weeks.previous_week),
        generated_at=generated_at,
        analyzed_article_count=analyzed_article_count,
        category_trends=[_to_category_trends(c) for c in bundle.category_trends],
    )


def _to_date_range(week: Week) -> _DateRange:
    return _DateRange(start=week.start, end=week.end)


def _to_category_trends(category_trends: CategoryTrends) -> _CategoryTrends:
    return _CategoryTrends(
        category=Category(
            slug=category_trends.category_slug, name=category_trends.category_name
        ),
        mention_trends=[_to_mention_trend(t) for t in category_trends.mention_trends],
    )


def _to_mention_trend(trend: MentionTrend) -> _MentionTrend:
    return _MentionTrend(
        name=trend.name,
        type=trend.type,
        article_volume=_MentionArticleVolume(
            count=trend.article_volume.count,
            previous_week_count=trend.article_volume.previous_week_count,
            rank=trend.article_volume.rank,
        ),
        growth=_MentionGrowth(rate=trend.growth.rate, rank=trend.growth.rank),
        key_points=list(trend.key_points),
        mentioned_with=[_to_co_mention(c) for c in trend.mentioned_with],
    )


def _to_co_mention(co_mention: CoMention) -> _CoMention:
    return _CoMention(
        name=co_mention.name,
        type=co_mention.type,
        shared_article_count=co_mention.shared_article_count,
    )
