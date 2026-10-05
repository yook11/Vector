"""トレンドの集約 (MentionCandidate / MentionTrend / CoMention / CategoryTrends /
TrendsBundle) と、週 (TrendWeeks) ・順位付け (rank_mention_trends) のテスト。

責務:
- VO 単体: 件数下限・文脈件数上限を構造的に強制する
- 伸び率: ``(count - previous_week_count) / max(previous_week_count, SMOOTHING)``
- 順位: 候補全体で同じ値は同じ順位 (1, 2, 2, 4)。どちらかの順位が上位に入った
  名前だけを、上位の境目の同点も含めて返す
- 週: スナップショットの日付の前日までの7日間と、その直前の7日間
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
from pydantic import ValidationError

from app.analysis.assessment.domain.result import MentionType
from app.insights.trend_discovery.domain.mention_name import MentionName
from app.insights.trend_discovery.domain.trend import (
    MAX_CATEGORIES_PER_BUNDLE,
    MAX_CO_MENTIONS,
    MAX_KEY_POINTS_PER_MENTION,
    MIN_CANDIDATE_COUNT,
    MIN_PREVIOUS_WEEK_COUNT,
    MIN_SHARED_ARTICLES,
    NEW_BURST_THRESHOLD,
    SMOOTHING,
    TOP_N_PER_RANKING,
    CategoryTrends,
    CoMention,
    MentionArticleVolume,
    MentionCandidate,
    MentionGrowth,
    MentionTrend,
    TrendsBundle,
    TrendWeeks,
    Week,
    rank_mention_trends,
)


def _candidate(
    name: str = "NVIDIA", *, count: int = 10, previous_week_count: int = 3
) -> MentionCandidate:
    return MentionCandidate(
        name=MentionName(name),
        type=MentionType.COMPANY,
        count=count,
        previous_week_count=previous_week_count,
    )


def _trend(name: str = "NVIDIA") -> MentionTrend:
    return MentionTrend(
        name=MentionName(name),
        type=MentionType.COMPANY,
        article_volume=MentionArticleVolume(count=10, previous_week_count=3, rank=1),
        growth=MentionGrowth(rate=2.333, rank=1),
    )


def _co_mention(name: str = "OpenAI", shared: int = 3) -> CoMention:
    return CoMention(
        name=MentionName(name),
        type=MentionType.COMPANY,
        shared_article_count=shared,
    )


def _ranks(trends: tuple[MentionTrend, ...]) -> dict[str, tuple[int, int | None]]:
    return {t.name.root: (t.article_volume.rank, t.growth.rank) for t in trends}


class TestMentionCandidate:
    def test_rejects_count_below_min(self) -> None:
        """週の記事数が MIN_CANDIDATE_COUNT 未満の名前は候補にならない。"""
        with pytest.raises(ValidationError):
            _candidate(count=MIN_CANDIDATE_COUNT - 1, previous_week_count=0)

    def test_accepts_count_at_min(self) -> None:
        candidate = _candidate(count=MIN_CANDIDATE_COUNT, previous_week_count=0)
        assert candidate.count == MIN_CANDIDATE_COUNT

    def test_rejects_negative_previous_week_count(self) -> None:
        with pytest.raises(ValidationError):
            _candidate(count=10, previous_week_count=-1)

    def test_immutable(self) -> None:
        candidate = _candidate()
        with pytest.raises(ValidationError):
            candidate.count = 99  # type: ignore[misc]

    def test_growth_rate_uses_smoothing_when_previous_week_is_zero(self) -> None:
        """前週0件のとき分母は SMOOTHING (除算回避)。"""
        assert _candidate(count=10, previous_week_count=0).growth_rate == (
            pytest.approx((10 - 0) / SMOOTHING)
        )

    def test_growth_rate_uses_previous_week_when_above_smoothing(self) -> None:
        """前週の記事数が SMOOTHING を超えるなら分母は前週の記事数そのもの。"""
        assert _candidate(count=20, previous_week_count=5).growth_rate == (
            pytest.approx((20 - 5) / 5)
        )

    def test_growth_rate_uses_smoothing_when_previous_week_below_smoothing(
        self,
    ) -> None:
        assert _candidate(count=10, previous_week_count=1).growth_rate == (
            pytest.approx((10 - 1) / SMOOTHING)
        )

    def test_previous_week_at_min_is_growth_ranked(self) -> None:
        """前週 MIN_PREVIOUS_WEEK_COUNT(2) 件の継続は伸び率で順位を付ける。"""
        candidate = _candidate(
            count=MIN_CANDIDATE_COUNT, previous_week_count=MIN_PREVIOUS_WEEK_COUNT
        )
        assert candidate.is_growth_ranked is True

    def test_small_previous_week_without_burst_is_not_growth_ranked(self) -> None:
        """前週1件・週9件は継続 (前週2件以上) も急増 (週10件以上) も満たさない。"""
        assert _candidate(count=9, previous_week_count=1).is_growth_ranked is False

    def test_zero_previous_week_at_burst_threshold_is_growth_ranked(self) -> None:
        candidate = _candidate(count=NEW_BURST_THRESHOLD, previous_week_count=0)
        assert candidate.is_growth_ranked is True

    def test_zero_previous_week_below_burst_threshold_is_not_growth_ranked(
        self,
    ) -> None:
        candidate = _candidate(count=NEW_BURST_THRESHOLD - 1, previous_week_count=0)
        assert candidate.is_growth_ranked is False


class TestCoMention:
    def test_constructs_at_min_shared(self) -> None:
        assert _co_mention(shared=MIN_SHARED_ARTICLES).shared_article_count == (
            MIN_SHARED_ARTICLES
        )

    def test_rejects_below_min_shared(self) -> None:
        """一緒に出た記事が1件 (< MIN_SHARED_ARTICLES) の名前は noise として除く。"""
        with pytest.raises(ValidationError):
            _co_mention(shared=MIN_SHARED_ARTICLES - 1)

    def test_immutable(self) -> None:
        co_mention = _co_mention()
        with pytest.raises(ValidationError):
            co_mention.shared_article_count = 99  # type: ignore[misc]


class TestMentionTrend:
    def test_context_defaults_empty(self) -> None:
        """要点と一緒に語られた名前を付ける前は空。"""
        trend = _trend()
        assert (trend.key_points, trend.mentioned_with) == ((), ())

    def test_rejects_too_many_key_points(self) -> None:
        with pytest.raises(ValidationError):
            MentionTrend.model_validate(
                {
                    **_trend().model_dump(),
                    "key_points": tuple(
                        f"kp {i}" for i in range(MAX_KEY_POINTS_PER_MENTION + 1)
                    ),
                }
            )

    def test_rejects_too_many_co_mentions(self) -> None:
        with pytest.raises(ValidationError):
            MentionTrend.model_validate(
                {
                    **_trend().model_dump(),
                    "mentioned_with": tuple(
                        _co_mention(f"peer {i}").model_dump()
                        for i in range(MAX_CO_MENTIONS + 1)
                    ),
                }
            )

    def test_growth_rank_can_be_missing(self) -> None:
        """伸び率で順位を付けない名前は growth.rank が None。"""
        assert MentionGrowth(rate=1.0, rank=None).rank is None

    def test_rejects_rank_below_one(self) -> None:
        with pytest.raises(ValidationError):
            MentionArticleVolume(count=10, previous_week_count=3, rank=0)


class TestRankMentionTrends:
    def test_same_count_shares_the_article_volume_rank(self) -> None:
        """記事数 20, 15, 15, 12 の順位は 1, 2, 2, 4。"""
        trends = rank_mention_trends(
            [
                _candidate("A", count=20, previous_week_count=20),
                _candidate("B", count=15, previous_week_count=15),
                _candidate("C", count=15, previous_week_count=15),
                _candidate("D", count=12, previous_week_count=12),
            ]
        )
        assert {k: v[0] for k, v in _ranks(trends).items()} == {
            "A": 1,
            "B": 2,
            "C": 2,
            "D": 4,
        }

    def test_same_growth_rate_shares_the_growth_rank(self) -> None:
        """伸び率 (20-4)/4 = (10-2)/2 = 4.0 は同じ順位、(8-4)/4 = 1.0 はその次の次。"""
        trends = rank_mention_trends(
            [
                _candidate("High", count=20, previous_week_count=4),
                _candidate("Low", count=10, previous_week_count=2),
                _candidate("Slow", count=8, previous_week_count=4),
            ]
        )
        assert {k: v[1] for k, v in _ranks(trends).items()} == {
            "High": 1,
            "Low": 1,
            "Slow": 3,
        }

    def test_includes_every_name_tied_at_the_boundary(self) -> None:
        """記事数5位に3つが並ぶと、伸び率で6位の F・G も記事数5位として載る。"""
        # (週, 前週): 伸び率は A 2.0, B 1.5, C 1.0, D 0.5, E 0.25, F・G 0.0
        counts = {
            "A": (30, 10),
            "B": (25, 10),
            "C": (20, 10),
            "D": (15, 10),
            "E": (10, 8),
            "F": (10, 10),
            "G": (10, 10),
        }
        trends = rank_mention_trends(
            [
                _candidate(n, count=c, previous_week_count=p)
                for n, (c, p) in counts.items()
            ]
        )
        assert _ranks(trends) == {
            "A": (1, 1),
            "B": (2, 2),
            "C": (3, 3),
            "D": (4, 4),
            "E": (5, 5),
            "F": (5, 6),
            "G": (5, 6),
        }

    def test_growth_rank_is_none_when_not_growth_ranked(self) -> None:
        """前週1件・週9件の名前は記事数で上位でも伸び率の順位を持たない。"""
        trends = rank_mention_trends(
            [_candidate("Quiet", count=9, previous_week_count=1)]
        )
        assert _ranks(trends) == {"Quiet": (1, None)}

    def test_excludes_names_outside_both_rankings(self) -> None:
        """記事数6位で伸び率の順位も持たない名前は載らない。"""
        steady = {"A": 30, "B": 25, "C": 20, "D": 15, "E": 12}
        candidates = [
            _candidate(n, count=c, previous_week_count=c) for n, c in steady.items()
        ]
        candidates.append(_candidate("Small", count=9, previous_week_count=1))
        trends = rank_mention_trends(candidates)
        assert "Small" not in _ranks(trends)

    def test_includes_names_ranked_only_by_growth(self) -> None:
        """記事数6位でも伸び率が上位なら、その記事数の順位のまま載る。"""
        steady = {"A": 30, "B": 25, "C": 20, "D": 15, "E": 12}
        candidates = [
            _candidate(n, count=c, previous_week_count=c) for n, c in steady.items()
        ]
        candidates.append(_candidate("Rising", count=8, previous_week_count=2))
        trends = rank_mention_trends(candidates)
        assert _ranks(trends)["Rising"] == (6, 1)

    def test_orders_by_article_volume_rank_then_name(self) -> None:
        trends = rank_mention_trends(
            [
                _candidate("zeta", count=10, previous_week_count=10),
                _candidate("Alpha", count=10, previous_week_count=10),
                _candidate("Top", count=20, previous_week_count=20),
            ]
        )
        assert [t.name.root for t in trends] == ["Top", "Alpha", "zeta"]

    def test_keeps_counts_and_growth_rate(self) -> None:
        (trend,) = rank_mention_trends(
            [_candidate("NVIDIA", count=12, previous_week_count=4)]
        )
        assert (trend.article_volume, trend.growth) == (
            MentionArticleVolume(count=12, previous_week_count=4, rank=1),
            MentionGrowth(rate=(12 - 4) / 4, rank=1),
        )

    def test_empty_candidates_return_empty_tuple(self) -> None:
        assert rank_mention_trends([]) == ()


class TestCategoryTrends:
    def test_accepts_more_names_than_top_n_when_tied(self) -> None:
        """同点で上位が増えるため、件数の上限は持たない。"""
        trends = tuple(_trend(f"m{i}") for i in range(TOP_N_PER_RANKING + 2))
        category_trends = CategoryTrends(
            category_slug="ai_ml", category_name="AI・ML", mention_trends=trends
        )
        assert len(category_trends.mention_trends) == TOP_N_PER_RANKING + 2

    def test_immutable_aggregate(self) -> None:
        category_trends = CategoryTrends(
            category_slug="ai_ml", category_name="AI・ML", mention_trends=()
        )
        with pytest.raises(ValidationError):
            category_trends.category_slug = "other"  # type: ignore[misc]


class TestTrendsBundle:
    def _category_trends(self, slug: str = "ai_ml") -> CategoryTrends:
        return CategoryTrends(
            category_slug=slug, category_name="AI・ML", mention_trends=()
        )

    def test_rejects_too_many_category_trends(self) -> None:
        """category_trends は MAX_CATEGORIES_PER_BUNDLE 件まで。"""
        too_many = tuple(
            self._category_trends(f"c{i}") for i in range(MAX_CATEGORIES_PER_BUNDLE + 1)
        )
        with pytest.raises(ValidationError):
            TrendsBundle(
                weeks=TrendWeeks(snapshot_date=date(2026, 5, 3)),
                category_trends=too_many,
            )

    def test_model_dump_round_trip(self) -> None:
        """model_dump(mode='json') → model_validate で同値に戻る。

        永続化される実体は TrendsBundle の dump ではなく Trends レスポンス payload
        (camelCase) であることに注意。この round-trip はドメイン VO の不変条件検証。
        """
        enriched = _trend().model_copy(
            update={
                "key_points": ("AI chip demand surges",),
                "mentioned_with": (_co_mention(),),
            }
        )
        original = TrendsBundle(
            weeks=TrendWeeks(snapshot_date=date(2026, 5, 3)),
            category_trends=(
                CategoryTrends(
                    category_slug="ai_ml",
                    category_name="AI・ML",
                    mention_trends=(enriched,),
                ),
            ),
        )
        assert TrendsBundle.model_validate(original.model_dump(mode="json")) == (
            original
        )


class TestDomainConstants:
    """集計しきい値が想定値であることを pin する (仕様値のドリフト検出)。"""

    def test_min_candidate_count_is_five(self) -> None:
        assert MIN_CANDIDATE_COUNT == 5

    def test_smoothing_is_two(self) -> None:
        assert SMOOTHING == 2

    def test_min_shared_articles_is_two(self) -> None:
        assert MIN_SHARED_ARTICLES == 2

    def test_top_n_per_ranking_is_five(self) -> None:
        assert TOP_N_PER_RANKING == 5


class TestTrendWeeks:
    @pytest.mark.parametrize("day", [20, 21, 25, 26])
    def test_week_ends_the_day_before_on_any_weekday(self, day):
        """週は曜日によらず、スナップショットの日付の前日を最終日にする。"""
        weeks = TrendWeeks(snapshot_date=date(2026, 4, day))
        assert weeks.week == Week(
            start=date(2026, 4, day - 7), end=date(2026, 4, day - 1)
        )

    @pytest.mark.parametrize(
        ("now", "expected"),
        [
            (datetime(2026, 4, 30, 14, 59, 59, tzinfo=UTC), date(2026, 4, 30)),
            (datetime(2026, 4, 30, 15, 0, tzinfo=UTC), date(2026, 5, 1)),
            (datetime(2026, 4, 30, 15, 5, tzinfo=UTC), date(2026, 5, 1)),
            (datetime(2026, 5, 1, 14, 59, tzinfo=UTC), date(2026, 5, 1)),
        ],
    )
    def test_latest_snapshot_date_changes_only_at_jst_midnight(self, now, expected):
        """UTCで渡された時刻でもJST午前0時を境界にスナップショットの日付を決める。"""
        assert TrendWeeks.latest(now).snapshot_date == expected

    def test_rejects_a_current_time_without_timezone(self):
        """タイムゾーンのない時刻から暗黙に実行環境の時差を使わない。"""
        with pytest.raises(ValueError, match="timezone-aware"):
            TrendWeeks.latest(datetime(2026, 5, 1))

    def test_week_is_seven_days_across_year_end(self):
        """年をまたいでも週はJST午前0時で区切る7日間で、最終日を含む。"""
        week = TrendWeeks(snapshot_date=date(2026, 1, 3)).week
        assert (week.start, week.end) == (date(2025, 12, 27), date(2026, 1, 2))

    def test_published_range_is_jst_midnight_half_open(self):
        """公開日時は初日の0時 (含む) から最終日の翌日0時 (含まない) まで。"""
        week = TrendWeeks(snapshot_date=date(2026, 1, 3)).week
        assert (week.published_from, week.published_before) == (
            datetime(2025, 12, 26, 15, tzinfo=UTC),
            datetime(2026, 1, 2, 15, tzinfo=UTC),
        )

    def test_previous_week_immediately_precedes_the_week(self):
        """前週は週の初日の前日までの7日間。"""
        weeks = TrendWeeks(snapshot_date=date(2026, 5, 3))
        assert weeks.previous_week == Week(
            start=date(2026, 4, 19), end=date(2026, 4, 25)
        )

    def test_weeks_are_immutable(self):
        """準備と集計の間に週を書き換えられない。"""
        weeks = TrendWeeks(snapshot_date=date(2026, 5, 3))
        with pytest.raises(ValidationError):
            weeks.snapshot_date = date(2026, 5, 4)  # type: ignore[misc]
