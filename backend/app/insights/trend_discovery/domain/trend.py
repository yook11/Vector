"""トレンドの集計期間・順位・集計結果を定義する。"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, date, datetime, time, timedelta
from typing import Final, Self
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field

from app.analysis.assessment.domain.result import MentionType
from app.insights.trend_discovery.domain.mention_name import MentionName

TREND_TZ: Final[str] = "Asia/Tokyo"
_WEEK_DAYS: Final[int] = 7


def _jst_midnight(day: date) -> datetime:
    return datetime.combine(day, time.min, tzinfo=ZoneInfo(TREND_TZ)).astimezone(UTC)


class Week(BaseModel):
    """JSTの初日から最終日までの7日間。"""

    model_config = ConfigDict(frozen=True)

    start: date
    end: date

    @property
    def published_from(self) -> datetime:
        """公開日時の下限 (含む)。"""
        return _jst_midnight(self.start)

    @property
    def published_before(self) -> datetime:
        """公開日時の上限 (含まない)。"""
        return _jst_midnight(self.end + timedelta(days=1))


def _week_before(day: date) -> Week:
    return Week(start=day - timedelta(days=_WEEK_DAYS), end=day - timedelta(days=1))


class TrendWeeks(BaseModel):
    """スナップショットの日付 (JST) から決まる、集計する週とその前週。

    週はスナップショットの日付の前日までの7日間で、暦の週ではない。
    """

    model_config = ConfigDict(frozen=True)

    snapshot_date: date

    @classmethod
    def latest(cls, now: datetime) -> Self:
        """現在日時をJSTへ揃え、直近の完了済みの週を決める。"""
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("now must be timezone-aware")
        return cls(snapshot_date=now.astimezone(ZoneInfo(TREND_TZ)).date())

    @property
    def week(self) -> Week:
        return _week_before(self.snapshot_date)

    @property
    def previous_week(self) -> Week:
        return _week_before(self.week.start)


MIN_CANDIDATE_COUNT: Final[int] = 5
MIN_PREVIOUS_WEEK_COUNT: Final[int] = 2
NEW_BURST_THRESHOLD: Final[int] = 10
# 前週の記事数が少ない場合の伸び率を抑える。
SMOOTHING: Final[int] = 2

TOP_N_PER_RANKING: Final[int] = 5
MAX_CATEGORIES_PER_BUNDLE: Final[int] = 20

MAX_KEY_POINTS_PER_MENTION: Final[int] = 3
MAX_CO_MENTIONS: Final[int] = 3
MIN_SHARED_ARTICLES: Final[int] = 2

# 異常な集計値を検出するための上限。
_MAX_COUNT: Final[int] = 10_000

# 正規化した名前と種別で同一メンションを識別する。
MentionKey = tuple[str, str]


class MentionCandidate(BaseModel):
    """週に最低記事数以上で登場し、順位を付ける対象になる名前。"""

    model_config = ConfigDict(frozen=True)

    name: MentionName
    type: MentionType
    count: int = Field(ge=MIN_CANDIDATE_COUNT, le=_MAX_COUNT)
    previous_week_count: int = Field(ge=0, le=_MAX_COUNT)

    @property
    def growth_rate(self) -> float:
        return (self.count - self.previous_week_count) / max(
            self.previous_week_count, SMOOTHING
        )

    @property
    def is_growth_ranked(self) -> bool:
        """前週の実績か週の急増があるときだけ、伸び率で順位を付ける。"""
        return (
            self.previous_week_count >= MIN_PREVIOUS_WEEK_COUNT
            or self.count >= NEW_BURST_THRESHOLD
        )


class MentionArticleVolume(BaseModel):
    """名前が要点に出てきた記事の数と、その順位。"""

    model_config = ConfigDict(frozen=True)

    count: int = Field(ge=MIN_CANDIDATE_COUNT, le=_MAX_COUNT)
    previous_week_count: int = Field(ge=0, le=_MAX_COUNT)
    rank: int = Field(ge=1)


class MentionGrowth(BaseModel):
    """前週からの伸び率と、その順位。"""

    model_config = ConfigDict(frozen=True)

    rate: float
    # 伸び率で順位を付ける条件を満たさない名前は None。
    rank: int | None = Field(ge=1)


class CoMention(BaseModel):
    """同じ要点に一緒に出てきた名前と、その記事数。"""

    model_config = ConfigDict(frozen=True)

    name: MentionName
    type: MentionType
    shared_article_count: int = Field(ge=MIN_SHARED_ARTICLES, le=_MAX_COUNT)


class MentionTrend(BaseModel):
    """名前1つの週のトレンド。"""

    model_config = ConfigDict(frozen=True)

    name: MentionName
    type: MentionType
    article_volume: MentionArticleVolume
    growth: MentionGrowth
    key_points: tuple[str, ...] = Field(
        default=(), max_length=MAX_KEY_POINTS_PER_MENTION
    )
    mentioned_with: tuple[CoMention, ...] = Field(
        default=(), max_length=MAX_CO_MENTIONS
    )


def rank_mention_trends(
    candidates: Iterable[MentionCandidate],
) -> tuple[MentionTrend, ...]:
    """候補全体で記事数と伸び率の順位を付け、どちらかが上位に入った名前を返す。

    同じ値は同じ順位 (1, 2, 2, 4) にし、上位の境目で同点の名前はすべて含める。
    """
    pool = tuple(candidates)
    counts = [c.count for c in pool]
    growth_rates = [c.growth_rate for c in pool if c.is_growth_ranked]
    trends: list[MentionTrend] = []
    for candidate in pool:
        volume_rank = 1 + sum(1 for count in counts if count > candidate.count)
        growth_rank = (
            1 + sum(1 for rate in growth_rates if rate > candidate.growth_rate)
            if candidate.is_growth_ranked
            else None
        )
        if volume_rank > TOP_N_PER_RANKING and (
            growth_rank is None or growth_rank > TOP_N_PER_RANKING
        ):
            continue
        trends.append(
            MentionTrend(
                name=candidate.name,
                type=candidate.type,
                article_volume=MentionArticleVolume(
                    count=candidate.count,
                    previous_week_count=candidate.previous_week_count,
                    rank=volume_rank,
                ),
                growth=MentionGrowth(rate=candidate.growth_rate, rank=growth_rank),
            )
        )
    return tuple(
        sorted(
            trends,
            key=lambda t: (t.article_volume.rank, t.name.match_key, t.type.value),
        )
    )


class CategoryTrends(BaseModel):
    """1カテゴリの週のトレンド。"""

    model_config = ConfigDict(frozen=True)

    category_slug: str
    category_name: str
    mention_trends: tuple[MentionTrend, ...]


class TrendsBundle(BaseModel):
    """集計した週と、全カテゴリのトレンド。"""

    model_config = ConfigDict(frozen=True)

    weeks: TrendWeeks
    category_trends: tuple[CategoryTrends, ...] = Field(
        max_length=MAX_CATEGORIES_PER_BUNDLE
    )
