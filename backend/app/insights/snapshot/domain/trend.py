"""週次トレンドの値オブジェクトと集約ルート。

公開モデル:
- ``EntityTrend`` / ``TopicTrend``: hot 判定済み 1 件分の集計結果
  (件数 + 派生 hotness_score)
- ``NewEntity``: 過去 lookback 週に出現履歴のない初出エンティティ
- ``WeeklyCategoryTrends`` (集約ルート): 1 カテゴリ × 1 週分のトレンド束
- ``WeeklyTrendsBundle`` (snapshot 永続形): 1 週分の全カテゴリ集約

責務:
- 件数の下限/非負を Pydantic ``Field(ge=...)`` で構造的に強制
  (ランタイム if より構造で守る: feedback_structural_guarantee.md)
- 集約は ``frozen=True`` + ``tuple[...]`` 子コレクションで深く immutable
- ``WeeklyTrendsBundle.model_dump(mode="json")`` 結果をそのまま JSONB に保存し、
  ``model_validate`` で復元できる (snapshot は 1 単位保存:
  feedback_snapshot_responsibility.md)

hotness_score:
  ``(current_count - previous_count) / max(previous_count, SMOOTHING)``
  - 前週 0 でも除算回避
  - 前週 < SMOOTHING でも分母が SMOOTHING に置き換わるため burst の過大評価を抑える
"""

from __future__ import annotations

from datetime import date

from pydantic import BaseModel, ConfigDict, Field, computed_field

from app.analysis.domain.value_objects.entity import EntityName, EntityType
from app.analysis.domain.value_objects.topic import TopicName
from app.domain.category import CategoryName, CategorySlug
from app.insights.snapshot.config import MIN_CURRENT, SMOOTHING


def _hotness(current: int, previous: int) -> float:
    return (current - previous) / max(previous, SMOOTHING)


class EntityTrend(BaseModel):
    """hot 判定済みエンティティ 1 件分の週次集計結果。

    Invariants (Pydantic Field 制約):
    - ``current_count >= MIN_CURRENT`` (noise 除去)
    - ``previous_count >= 0``
    - frozen
    """

    model_config = ConfigDict(frozen=True)

    name: EntityName
    type: EntityType
    current_count: int = Field(ge=MIN_CURRENT)
    previous_count: int = Field(ge=0)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def hotness_score(self) -> float:
        return _hotness(self.current_count, self.previous_count)


class TopicTrend(BaseModel):
    """hot 判定済みトピック 1 件分の週次集計結果。

    Invariants (Pydantic Field 制約):
    - ``current_count >= MIN_CURRENT``
    - ``previous_count >= 0``
    - frozen
    """

    model_config = ConfigDict(frozen=True)

    topic: TopicName
    current_count: int = Field(ge=MIN_CURRENT)
    previous_count: int = Field(ge=0)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def hotness_score(self) -> float:
        return _hotness(self.current_count, self.previous_count)


class NewEntity(BaseModel):
    """過去 ``NEW_ENTITY_LOOKBACK_WEEKS`` 週に出現履歴のない初出エンティティ。

    Invariants:
    - ``current_count >= 1`` (0 件なら NewEntity ではない)
    - frozen
    """

    model_config = ConfigDict(frozen=True)

    name: EntityName
    type: EntityType
    current_count: int = Field(ge=1)


class WeeklyCategoryTrends(BaseModel):
    """1 カテゴリ × 1 週分のトレンド束 (集約ルート)。

    集約配下のリストは ``tuple[...]`` で保持し、変更不可性を構造で保証する
    (feedback_aggregate_over_individual_vo.md)。
    """

    model_config = ConfigDict(frozen=True)

    category_id: int
    category_slug: CategorySlug
    category_name: CategoryName
    trending_entities: tuple[EntityTrend, ...]
    trending_topics: tuple[TopicTrend, ...]
    new_entities: tuple[NewEntity, ...]


class WeeklyTrendsBundle(BaseModel):
    """1 週分の全カテゴリトレンドをまとめた snapshot 永続形。

    ``model_dump(mode="json")`` 出力をそのまま JSONB に保存する。
    snapshot は 1 単位保存が責務 (feedback_snapshot_responsibility.md)。
    """

    model_config = ConfigDict(frozen=True)

    week_start: date
    sections: tuple[WeeklyCategoryTrends, ...]
