"""取得済みの事実からトレンドの生成開始条件を判定する。"""

from dataclasses import dataclass
from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field

from app.insights.trend_discovery.domain.trend import TrendWeeks


class TrendDiscoveryReadyBuildRejectionReason(StrEnum):
    """トレンドの生成を始めない理由。"""

    ALREADY_GENERATED = "already_generated"
    NO_ARTICLES = "no_articles"


@dataclass(frozen=True, slots=True)
class TrendDiscoveryReadyBuildFacts:
    """生成済みなら記事数を取得せず、Noneで未取得を表す。"""

    already_generated: bool
    analyzed_article_count: int | None


class ReadyForTrendDiscovery(BaseModel):
    """未生成かつ対象記事が存在する期間の、トレンド生成入力。"""

    model_config = ConfigDict(frozen=True)

    weeks: TrendWeeks
    analyzed_article_count: int = Field(gt=0)

    @classmethod
    def from_facts(
        cls, *, weeks: TrendWeeks, facts: TrendDiscoveryReadyBuildFacts
    ) -> Self | TrendDiscoveryReadyBuildRejectionReason:
        """DB問い合わせを行わず、生成済みを優先して開始条件を判定する。"""
        if facts.already_generated:
            return TrendDiscoveryReadyBuildRejectionReason.ALREADY_GENERATED
        if facts.analyzed_article_count is None:
            raise ValueError("analyzed_article_count is required for a new week")
        if facts.analyzed_article_count == 0:
            return TrendDiscoveryReadyBuildRejectionReason.NO_ARTICLES
        return cls(weeks=weeks, analyzed_article_count=facts.analyzed_article_count)
