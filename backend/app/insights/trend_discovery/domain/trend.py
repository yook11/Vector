"""トレンドの集計期間・ランキング・集計結果を定義する。"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, date, datetime, time, timedelta
from typing import Final, Self
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, computed_field

from app.analysis.assessment.domain.result import MentionType
from app.insights.trend_discovery.domain.mention_name import MentionName

TREND_TZ: Final[str] = "Asia/Tokyo"
_WEEK: Final[timedelta] = timedelta(days=7)


class TrendWindow(BaseModel):
    """JSTの終了日を境界とする完了済み7日間と、その直前の比較期間。"""

    model_config = ConfigDict(frozen=True)

    window_end: date

    @classmethod
    def latest(cls, now: datetime) -> Self:
        """現在日時をJSTへ揃え、直近の完了済み期間を決める。"""
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("now must be timezone-aware")
        return cls(window_end=now.astimezone(ZoneInfo(TREND_TZ)).date())

    @property
    def window_start(self) -> date:
        return self.window_end - _WEEK

    @property
    def current_end(self) -> datetime:
        return datetime.combine(
            self.window_end, time.min, tzinfo=ZoneInfo(TREND_TZ)
        ).astimezone(UTC)

    @property
    def current_start(self) -> datetime:
        return self.current_end - _WEEK

    @property
    def previous_start(self) -> datetime:
        return self.current_start - _WEEK


MIN_CURRENT: Final[int] = 5
MIN_PREVIOUS: Final[int] = 2
NEW_BURST_THRESHOLD: Final[int] = 10
# 前期間の記事数が少ない場合の伸び率を抑える。
SMOOTHING: Final[int] = 2

TOP_N_PER_RANKING: Final[int] = 5
MAX_CATEGORIES_PER_BUNDLE: Final[int] = 20

MAX_KEY_POINTS_PER_MENTION: Final[int] = 3
MAX_RELATED_MENTIONS: Final[int] = 3
MIN_SHARED_ARTICLES: Final[int] = 2

# 異常な集計値を検出するための上限。
_MAX_COUNT: Final[int] = 10_000

# 正規化した名前と種別で同一メンションを識別する。
MentionKey = tuple[str, str]


def _hotness(current: int, previous: int) -> float:
    return (current - previous) / max(previous, SMOOTHING)


def is_hot(mention: RankedMention) -> bool:
    """前期間の実績か今期間の急増を条件に、伸び率ランキングの対象を判定する。"""
    return (
        mention.previous_appearance_count >= MIN_PREVIOUS
        or mention.appearance_count >= NEW_BURST_THRESHOLD
    )


def select_most_mentioned(pool: Iterable[RankedMention]) -> tuple[RankedMention, ...]:
    """最低出現数を満たした候補から、出現回数の上位を選ぶ。"""
    return tuple(
        sorted(
            pool,
            key=lambda m: (-m.appearance_count, -m.hotness_score, m.name.match_key),
        )[:TOP_N_PER_RANKING]
    )


def select_fastest_growing(pool: Iterable[RankedMention]) -> tuple[RankedMention, ...]:
    """継続・急増の条件を満たした候補から、伸び率の上位を選ぶ。"""
    return tuple(
        sorted(
            (m for m in pool if is_hot(m)),
            key=lambda m: (-m.hotness_score, -m.appearance_count, m.name.match_key),
        )[:TOP_N_PER_RANKING]
    )


class RelatedMention(BaseModel):
    """同じ要点内で共起したメンションと、その記事数。"""

    model_config = ConfigDict(frozen=True)

    name: MentionName
    type: MentionType
    shared_article_count: int = Field(ge=MIN_SHARED_ARTICLES, le=_MAX_COUNT)


class RankedMention(BaseModel):
    """出現記事数・伸び率と、要点・関連メンションを持つランキング候補。"""

    model_config = ConfigDict(frozen=True)

    name: MentionName
    type: MentionType
    appearance_count: int = Field(ge=MIN_CURRENT, le=_MAX_COUNT)
    previous_appearance_count: int = Field(ge=0, le=_MAX_COUNT)
    key_points: tuple[str, ...] = Field(
        default=(), max_length=MAX_KEY_POINTS_PER_MENTION
    )
    related_mentions: tuple[RelatedMention, ...] = Field(
        default=(), max_length=MAX_RELATED_MENTIONS
    )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def hotness_score(self) -> float:
        return _hotness(self.appearance_count, self.previous_appearance_count)


class CategoryTrends(BaseModel):
    """1カテゴリ・1期間の出現回数と伸び率のランキング。"""

    model_config = ConfigDict(frozen=True)

    category_id: int
    category_slug: str
    category_name: str
    most_mentioned: tuple[RankedMention, ...] = Field(max_length=TOP_N_PER_RANKING)
    fastest_growing: tuple[RankedMention, ...] = Field(max_length=TOP_N_PER_RANKING)


class TrendsBundle(BaseModel):
    """対象期間と全カテゴリのトレンド集計結果。"""

    model_config = ConfigDict(frozen=True)

    window: TrendWindow
    category_trends: tuple[CategoryTrends, ...] = Field(
        max_length=MAX_CATEGORIES_PER_BUNDLE
    )
