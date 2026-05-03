"""digest 集約 (EntityTrend / TopicTrend / NewEntity / WeeklyCategoryTrends /
WeeklyTrendsBundle) の不変条件と派生フィールドのテスト。

責務:
- VO 単体: hot 判定の構成要素 (件数下限) を構造的に強制する
- 集約ルート (WeeklyCategoryTrends / WeeklyTrendsBundle): immutable な tuple で
  子リストを保持し、永続化形 (model_dump) と一致する
- hotness_score: ``(current - previous) / max(previous, SMOOTHING)`` で
  smoothing を適用 (前週 0 でも除算回避、burst を過大評価しすぎない)
"""

from __future__ import annotations

from datetime import date

import pytest
from pydantic import ValidationError

from app.analysis.domain.value_objects.entity import EntityName, EntityType
from app.analysis.domain.value_objects.topic import TopicName
from app.domain.category import CategoryName, CategorySlug
from app.insights.snapshot.config import (
    MIN_CURRENT,
    NEW_ENTITY_LOOKBACK_WEEKS,
    SMOOTHING,
)
from app.insights.snapshot.domain.trend import (
    EntityTrend,
    NewEntity,
    TopicTrend,
    WeeklyCategoryTrends,
    WeeklyTrendsBundle,
)


def _entity(
    name: str = "NVIDIA", type_: str = "company"
) -> tuple[EntityName, EntityType]:
    return EntityName(name), EntityType(type_)


class TestEntityTrend:
    def test_constructs_with_valid_counts(self) -> None:
        name, type_ = _entity()
        trend = EntityTrend(name=name, type=type_, current_count=10, previous_count=3)
        assert trend.name == name
        assert trend.type == type_
        assert trend.current_count == 10
        assert trend.previous_count == 3

    def test_rejects_current_below_min(self) -> None:
        """current_count < MIN_CURRENT は構造的に reject。"""
        name, type_ = _entity()
        with pytest.raises(ValidationError):
            EntityTrend(
                name=name,
                type=type_,
                current_count=MIN_CURRENT - 1,
                previous_count=0,
            )

    def test_accepts_current_at_min(self) -> None:
        name, type_ = _entity()
        trend = EntityTrend(
            name=name, type=type_, current_count=MIN_CURRENT, previous_count=0
        )
        assert trend.current_count == MIN_CURRENT

    def test_rejects_negative_previous(self) -> None:
        name, type_ = _entity()
        with pytest.raises(ValidationError):
            EntityTrend(name=name, type=type_, current_count=10, previous_count=-1)

    def test_accepts_previous_zero(self) -> None:
        """新規 burst (previous=0) は許容。hot 判定は集計側で行う。"""
        name, type_ = _entity()
        trend = EntityTrend(name=name, type=type_, current_count=20, previous_count=0)
        assert trend.previous_count == 0

    def test_immutable(self) -> None:
        name, type_ = _entity()
        trend = EntityTrend(name=name, type=type_, current_count=10, previous_count=3)
        with pytest.raises(ValidationError):
            trend.current_count = 99  # type: ignore[misc]

    def test_hotness_score_uses_smoothing_when_previous_is_zero(self) -> None:
        """previous_count=0 のとき分母は SMOOTHING (除算回避)。"""
        name, type_ = _entity()
        trend = EntityTrend(name=name, type=type_, current_count=10, previous_count=0)
        assert trend.hotness_score == pytest.approx((10 - 0) / SMOOTHING)

    def test_hotness_score_uses_previous_when_above_smoothing(self) -> None:
        """previous_count > SMOOTHING なら分母は previous_count。"""
        name, type_ = _entity()
        trend = EntityTrend(name=name, type=type_, current_count=20, previous_count=5)
        assert trend.hotness_score == pytest.approx((20 - 5) / 5)

    def test_hotness_score_uses_smoothing_when_previous_below_smoothing(
        self,
    ) -> None:
        """previous_count < SMOOTHING なら分母は SMOOTHING。"""
        name, type_ = _entity()
        # SMOOTHING = 2 を前提
        trend = EntityTrend(name=name, type=type_, current_count=10, previous_count=1)
        assert trend.hotness_score == pytest.approx((10 - 1) / SMOOTHING)


class TestTopicTrend:
    def test_constructs_with_valid_counts(self) -> None:
        trend = TopicTrend(
            topic=TopicName("ai agents"), current_count=8, previous_count=2
        )
        assert trend.topic.root == "ai agents"
        assert trend.current_count == 8
        assert trend.previous_count == 2

    def test_rejects_current_below_min(self) -> None:
        with pytest.raises(ValidationError):
            TopicTrend(
                topic=TopicName("ai agents"),
                current_count=MIN_CURRENT - 1,
                previous_count=0,
            )

    def test_rejects_negative_previous(self) -> None:
        with pytest.raises(ValidationError):
            TopicTrend(
                topic=TopicName("ai agents"),
                current_count=10,
                previous_count=-1,
            )

    def test_immutable(self) -> None:
        trend = TopicTrend(
            topic=TopicName("ai agents"), current_count=8, previous_count=2
        )
        with pytest.raises(ValidationError):
            trend.previous_count = 99  # type: ignore[misc]

    def test_hotness_score_smoothing(self) -> None:
        trend = TopicTrend(
            topic=TopicName("quantum computing"),
            current_count=12,
            previous_count=0,
        )
        assert trend.hotness_score == pytest.approx(12 / SMOOTHING)


class TestNewEntity:
    def test_constructs_with_count_at_least_one(self) -> None:
        name, type_ = _entity("DeepSeek-R1", "product")
        new = NewEntity(name=name, type=type_, current_count=1)
        assert new.current_count == 1

    def test_rejects_zero_count(self) -> None:
        """新規エンティティは少なくとも 1 件の登場が必要。

        0 件なら NewEntity ではない。
        """
        name, type_ = _entity()
        with pytest.raises(ValidationError):
            NewEntity(name=name, type=type_, current_count=0)

    def test_rejects_negative_count(self) -> None:
        name, type_ = _entity()
        with pytest.raises(ValidationError):
            NewEntity(name=name, type=type_, current_count=-1)

    def test_immutable(self) -> None:
        name, type_ = _entity()
        new = NewEntity(name=name, type=type_, current_count=3)
        with pytest.raises(ValidationError):
            new.current_count = 99  # type: ignore[misc]


class TestWeeklyCategoryTrends:
    def _make(
        self,
        *,
        entities: tuple[EntityTrend, ...] = (),
        topics: tuple[TopicTrend, ...] = (),
        new_entities: tuple[NewEntity, ...] = (),
    ) -> WeeklyCategoryTrends:
        return WeeklyCategoryTrends(
            category_id=1,
            category_slug=CategorySlug("ai_ml"),
            category_name=CategoryName("AI・ML"),
            trending_entities=entities,
            trending_topics=topics,
            new_entities=new_entities,
        )

    def test_constructs_with_empty_lists(self) -> None:
        section = self._make()
        assert section.category_id == 1
        assert section.category_slug.root == "ai_ml"
        assert section.category_name.root == "AI・ML"
        assert section.trending_entities == ()
        assert section.trending_topics == ()
        assert section.new_entities == ()

    def test_constructs_with_populated_lists(self) -> None:
        name, type_ = _entity()
        et = EntityTrend(name=name, type=type_, current_count=10, previous_count=3)
        tt = TopicTrend(topic=TopicName("ai agents"), current_count=8, previous_count=2)
        ne = NewEntity(name=name, type=type_, current_count=4)
        section = self._make(entities=(et,), topics=(tt,), new_entities=(ne,))
        assert section.trending_entities == (et,)
        assert section.trending_topics == (tt,)
        assert section.new_entities == (ne,)

    def test_immutable_aggregate(self) -> None:
        section = self._make()
        with pytest.raises(ValidationError):
            section.category_id = 99  # type: ignore[misc]

    def test_lists_are_tuples(self) -> None:
        """リストは tuple で永続化される (collections の immutability を構造で保証)。"""
        section = self._make()
        assert isinstance(section.trending_entities, tuple)
        assert isinstance(section.trending_topics, tuple)
        assert isinstance(section.new_entities, tuple)


class TestWeeklyTrendsBundle:
    def _section(self, category_id: int = 1) -> WeeklyCategoryTrends:
        return WeeklyCategoryTrends(
            category_id=category_id,
            category_slug=CategorySlug("ai_ml"),
            category_name=CategoryName("AI・ML"),
            trending_entities=(),
            trending_topics=(),
            new_entities=(),
        )

    def test_constructs_with_empty_sections(self) -> None:
        bundle = WeeklyTrendsBundle(window_end=date(2026, 5, 3), sections=())
        assert bundle.window_end == date(2026, 5, 3)
        assert bundle.sections == ()

    def test_constructs_with_multiple_sections(self) -> None:
        sections = (self._section(1), self._section(2))
        bundle = WeeklyTrendsBundle(window_end=date(2026, 5, 3), sections=sections)
        assert len(bundle.sections) == 2

    def test_immutable_bundle(self) -> None:
        bundle = WeeklyTrendsBundle(window_end=date(2026, 5, 3), sections=())
        with pytest.raises(ValidationError):
            bundle.window_end = date(2026, 4, 27)  # type: ignore[misc]

    def test_model_dump_round_trip(self) -> None:
        """model_dump(mode='json') → model_validate で同値に戻る (snapshot 永続化)。"""
        name, type_ = _entity()
        et = EntityTrend(name=name, type=type_, current_count=10, previous_count=3)
        section = WeeklyCategoryTrends(
            category_id=1,
            category_slug=CategorySlug("ai_ml"),
            category_name=CategoryName("AI・ML"),
            trending_entities=(et,),
            trending_topics=(),
            new_entities=(),
        )
        original = WeeklyTrendsBundle(window_end=date(2026, 5, 3), sections=(section,))
        dumped = original.model_dump(mode="json")
        restored = WeeklyTrendsBundle.model_validate(dumped)
        assert restored == original


class TestConfigConstants:
    """digest/config.py の定数が想定値であることを確認する (regression guard)。"""

    def test_min_current_is_five(self) -> None:
        assert MIN_CURRENT == 5

    def test_smoothing_is_two(self) -> None:
        assert SMOOTHING == 2

    def test_lookback_is_four_weeks(self) -> None:
        assert NEW_ENTITY_LOOKBACK_WEEKS == 4
