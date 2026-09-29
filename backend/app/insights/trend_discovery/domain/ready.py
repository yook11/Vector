"""取得済みの事実からトレンドの生成開始条件を判定する。"""

from dataclasses import dataclass
from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field

from app.insights.trend_discovery.domain.trend import TrendWindow


class TrendDiscoveryReadyBuildRejectionReason(StrEnum):
    """トレンドの生成を始めない理由。"""

    ALREADY_GENERATED = "already_generated"
    NO_ARTICLES = "no_articles"


@dataclass(frozen=True, slots=True)
class TrendDiscoveryReadyBuildFacts:
    """生成済みなら記事数を取得せず、Noneで未取得を表す。"""

    already_generated: bool
    source_analysis_count: int | None


class ReadyForTrendDiscovery(BaseModel):
    """未生成かつ対象記事が存在する期間の、トレンド生成入力。"""

    model_config = ConfigDict(frozen=True)

    window: TrendWindow
    source_analysis_count: int = Field(gt=0)

    @classmethod
    def from_facts(
        cls, *, window: TrendWindow, facts: TrendDiscoveryReadyBuildFacts
    ) -> Self | TrendDiscoveryReadyBuildRejectionReason:
        """DB問い合わせを行わず、生成済みを優先して開始条件を判定する。"""
        if facts.already_generated:
            return TrendDiscoveryReadyBuildRejectionReason.ALREADY_GENERATED
        if facts.source_analysis_count is None:
            raise ValueError("source_analysis_count is required for a new window")
        if facts.source_analysis_count == 0:
            return TrendDiscoveryReadyBuildRejectionReason.NO_ARTICLES
        return cls(window=window, source_analysis_count=facts.source_analysis_count)
