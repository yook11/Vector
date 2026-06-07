"""Trend Discovery 集約 (RankedMention / RelatedMention / CategoryTrends /
TrendsBundle) の不変条件と派生フィールドのテスト。

責務:
- VO 単体: 件数下限・文脈件数上限を構造的に強制する
- 集約ルート (CategoryTrends / TrendsBundle): immutable な tuple で
  子リストを保持し、各ランキングを top N に構造的に制限し、永続化形 (model_dump)
  と一致する
- hotness_score: ``(current - previous) / max(previous, SMOOTHING)`` で
  smoothing を適用 (前週 0 でも除算回避、burst を過大評価しすぎない)
"""

from __future__ import annotations

from datetime import date

import pytest
from pydantic import ValidationError

from app.analysis.assessment.domain.result import MentionType
from app.insights.trend_discovery.domain.mention_name import MentionName
from app.insights.trend_discovery.domain.trend import (
    MAX_KEY_POINTS_PER_MENTION,
    MAX_RELATED_MENTIONS,
    MIN_CURRENT,
    MIN_SHARED_ARTICLES,
    SMOOTHING,
    TOP_N_PER_RANKING,
    CategoryTrends,
    RankedMention,
    RelatedMention,
    TrendsBundle,
)
from app.models.value_objects.category import CategoryName, CategorySlug


def _names(
    name: str = "NVIDIA", type_: str = "company"
) -> tuple[MentionName, MentionType]:
    return MentionName(name), MentionType(type_)


def _mention(
    name: str = "NVIDIA", *, current: int = 10, previous: int = 3
) -> RankedMention:
    n, t = _names(name)
    return RankedMention(
        name=n, type=t, appearance_count=current, previous_appearance_count=previous
    )


class TestRankedMention:
    def test_constructs_with_valid_counts(self) -> None:
        name, type_ = _names()
        trend = RankedMention(
            name=name, type=type_, appearance_count=10, previous_appearance_count=3
        )
        assert trend.name == name
        assert trend.type == type_
        assert trend.appearance_count == 10
        assert trend.previous_appearance_count == 3

    def test_context_defaults_empty(self) -> None:
        """enrich 前の純集計段階では key_points / related_mentions は空。"""
        trend = _mention()
        assert trend.key_points == ()
        assert trend.related_mentions == ()

    def test_rejects_current_below_min(self) -> None:
        """appearance_count < MIN_CURRENT は構造的に reject。"""
        name, type_ = _names()
        with pytest.raises(ValidationError):
            RankedMention(
                name=name,
                type=type_,
                appearance_count=MIN_CURRENT - 1,
                previous_appearance_count=0,
            )

    def test_accepts_current_at_min(self) -> None:
        name, type_ = _names()
        trend = RankedMention(
            name=name,
            type=type_,
            appearance_count=MIN_CURRENT,
            previous_appearance_count=0,
        )
        assert trend.appearance_count == MIN_CURRENT

    def test_rejects_negative_previous(self) -> None:
        name, type_ = _names()
        with pytest.raises(ValidationError):
            RankedMention(
                name=name, type=type_, appearance_count=10, previous_appearance_count=-1
            )

    def test_accepts_previous_zero(self) -> None:
        """新規 burst (previous=0) は許容。hot 判定は集計側で行う。"""
        name, type_ = _names()
        trend = RankedMention(
            name=name, type=type_, appearance_count=20, previous_appearance_count=0
        )
        assert trend.previous_appearance_count == 0

    def test_rejects_too_many_key_points(self) -> None:
        """key_points は MAX_KEY_POINTS_PER_MENTION 本まで。"""
        name, type_ = _names()
        with pytest.raises(ValidationError):
            RankedMention(
                name=name,
                type=type_,
                appearance_count=10,
                previous_appearance_count=3,
                key_points=tuple(
                    f"kp {i}" for i in range(MAX_KEY_POINTS_PER_MENTION + 1)
                ),
            )

    def test_rejects_too_many_related_mentions(self) -> None:
        """related_mentions は MAX_RELATED_MENTIONS 件まで。"""
        name, type_ = _names()
        related = tuple(
            RelatedMention(
                name=MentionName(f"peer {i}"),
                type=MentionType.COMPANY,
                shared_article_count=MIN_SHARED_ARTICLES,
            )
            for i in range(MAX_RELATED_MENTIONS + 1)
        )
        with pytest.raises(ValidationError):
            RankedMention(
                name=name,
                type=type_,
                appearance_count=10,
                previous_appearance_count=3,
                related_mentions=related,
            )

    def test_immutable(self) -> None:
        trend = _mention()
        with pytest.raises(ValidationError):
            trend.appearance_count = 99  # type: ignore[misc]

    def test_hotness_score_uses_smoothing_when_previous_is_zero(self) -> None:
        """previous_appearance_count=0 のとき分母は SMOOTHING (除算回避)。"""
        name, type_ = _names()
        trend = RankedMention(
            name=name, type=type_, appearance_count=10, previous_appearance_count=0
        )
        assert trend.hotness_score == pytest.approx((10 - 0) / SMOOTHING)

    def test_hotness_score_uses_previous_when_above_smoothing(self) -> None:
        """previous_appearance_count > SMOOTHING なら分母は前週件数そのもの。"""
        name, type_ = _names()
        trend = RankedMention(
            name=name, type=type_, appearance_count=20, previous_appearance_count=5
        )
        assert trend.hotness_score == pytest.approx((20 - 5) / 5)

    def test_hotness_score_uses_smoothing_when_previous_below_smoothing(
        self,
    ) -> None:
        """previous_appearance_count < SMOOTHING なら分母は SMOOTHING。"""
        name, type_ = _names()
        # SMOOTHING = 2 を前提
        trend = RankedMention(
            name=name, type=type_, appearance_count=10, previous_appearance_count=1
        )
        assert trend.hotness_score == pytest.approx((10 - 1) / SMOOTHING)


class TestRelatedMention:
    def test_constructs_at_min_shared(self) -> None:
        related = RelatedMention(
            name=MentionName("OpenAI"),
            type=MentionType.COMPANY,
            shared_article_count=MIN_SHARED_ARTICLES,
        )
        assert related.shared_article_count == MIN_SHARED_ARTICLES

    def test_rejects_below_min_shared(self) -> None:
        """共起 1 記事 (< MIN_SHARED_ARTICLES) は noise として構造的に reject。"""
        with pytest.raises(ValidationError):
            RelatedMention(
                name=MentionName("OpenAI"),
                type=MentionType.COMPANY,
                shared_article_count=MIN_SHARED_ARTICLES - 1,
            )

    def test_immutable(self) -> None:
        related = RelatedMention(
            name=MentionName("OpenAI"),
            type=MentionType.COMPANY,
            shared_article_count=MIN_SHARED_ARTICLES,
        )
        with pytest.raises(ValidationError):
            related.shared_article_count = 99  # type: ignore[misc]


class TestCategoryTrends:
    def _make(
        self,
        *,
        most_mentioned: tuple[RankedMention, ...] = (),
        fastest_growing: tuple[RankedMention, ...] = (),
    ) -> CategoryTrends:
        return CategoryTrends(
            category_id=1,
            category_slug=CategorySlug("ai_ml"),
            category_name=CategoryName("AI・ML"),
            most_mentioned=most_mentioned,
            fastest_growing=fastest_growing,
        )

    def test_constructs_with_empty_rankings(self) -> None:
        category_trends = self._make()
        assert category_trends.category_id == 1
        assert category_trends.category_slug.root == "ai_ml"
        assert category_trends.category_name.root == "AI・ML"
        assert category_trends.most_mentioned == ()
        assert category_trends.fastest_growing == ()

    def test_constructs_with_populated_rankings(self) -> None:
        appearance = _mention("Appears")
        growth = _mention("Grows")
        category_trends = self._make(
            most_mentioned=(appearance,), fastest_growing=(growth,)
        )
        assert category_trends.most_mentioned == (appearance,)
        assert category_trends.fastest_growing == (growth,)

    def test_rejects_most_mentioned_over_top_n(self) -> None:
        """most_mentioned は TOP_N_PER_RANKING 件まで。"""
        too_many = tuple(_mention(f"m{i}") for i in range(TOP_N_PER_RANKING + 1))
        with pytest.raises(ValidationError):
            self._make(most_mentioned=too_many)

    def test_rejects_fastest_growing_over_top_n(self) -> None:
        """fastest_growing は TOP_N_PER_RANKING 件まで。"""
        too_many = tuple(_mention(f"m{i}") for i in range(TOP_N_PER_RANKING + 1))
        with pytest.raises(ValidationError):
            self._make(fastest_growing=too_many)

    def test_immutable_aggregate(self) -> None:
        category_trends = self._make()
        with pytest.raises(ValidationError):
            category_trends.category_id = 99  # type: ignore[misc]

    def test_rankings_are_tuples(self) -> None:
        """ランキングは tuple で永続化される (immutability を構造で保証)。"""
        category_trends = self._make()
        assert isinstance(category_trends.most_mentioned, tuple)
        assert isinstance(category_trends.fastest_growing, tuple)


class TestTrendsBundle:
    def _category_trends(self, category_id: int = 1) -> CategoryTrends:
        return CategoryTrends(
            category_id=category_id,
            category_slug=CategorySlug("ai_ml"),
            category_name=CategoryName("AI・ML"),
            most_mentioned=(),
            fastest_growing=(),
        )

    def test_constructs_with_empty_category_trends(self) -> None:
        bundle = TrendsBundle(window_end=date(2026, 5, 3), category_trends=())
        assert bundle.window_end == date(2026, 5, 3)
        assert bundle.category_trends == ()

    def test_constructs_with_multiple_category_trends(self) -> None:
        category_trends = (self._category_trends(1), self._category_trends(2))
        bundle = TrendsBundle(
            window_end=date(2026, 5, 3), category_trends=category_trends
        )
        assert len(bundle.category_trends) == 2

    def test_immutable_bundle(self) -> None:
        bundle = TrendsBundle(window_end=date(2026, 5, 3), category_trends=())
        with pytest.raises(ValidationError):
            bundle.window_end = date(2026, 4, 27)  # type: ignore[misc]

    def test_model_dump_round_trip(self) -> None:
        """model_dump(mode='json') → model_validate で同値に戻る (snapshot 永続化)。"""
        enriched = _mention("NVIDIA").model_copy(
            update={
                "key_points": ("AI chip demand surges",),
                "related_mentions": (
                    RelatedMention(
                        name=MentionName("OpenAI"),
                        type=MentionType.COMPANY,
                        shared_article_count=3,
                    ),
                ),
            }
        )
        category_trends = CategoryTrends(
            category_id=1,
            category_slug=CategorySlug("ai_ml"),
            category_name=CategoryName("AI・ML"),
            most_mentioned=(enriched,),
            fastest_growing=(enriched,),
        )
        original = TrendsBundle(
            window_end=date(2026, 5, 3), category_trends=(category_trends,)
        )
        dumped = original.model_dump(mode="json")
        restored = TrendsBundle.model_validate(dumped)
        assert restored == original


class TestDomainConstants:
    """集計しきい値が想定値であることを pin する (仕様値のドリフト検出)。"""

    def test_min_current_is_five(self) -> None:
        assert MIN_CURRENT == 5

    def test_smoothing_is_two(self) -> None:
        assert SMOOTHING == 2

    def test_min_shared_articles_is_two(self) -> None:
        assert MIN_SHARED_ARTICLES == 2

    def test_top_n_per_ranking_is_five(self) -> None:
        assert TOP_N_PER_RANKING == 5
