"""Trends schema の期待動作。

API レスポンスの keys は camelCase に揃える (Vector 全体規約)。集計結果から
週・カテゴリ・名前のトレンドへの変換をテスト境界で固定する。
"""

from __future__ import annotations

from datetime import UTC, date, datetime

from app.insights.trend_discovery.domain.trend import (
    CategoryTrends,
    CoMention,
    MentionArticleVolume,
    MentionGrowth,
    MentionTrend,
    TrendsBundle,
    TrendWeeks,
)
from app.insights.trend_discovery.schemas import trends_from_snapshot


class TestFromSnapshot:
    def test_publishes_weeks_category_and_mention_trends(self) -> None:
        """カテゴリは Category の形、名前は記事数と伸びのまとまりで出す。"""
        trend = MentionTrend(
            name="NVIDIA",
            type="company",
            article_volume=MentionArticleVolume(
                count=30, previous_week_count=5, rank=1
            ),
            growth=MentionGrowth(rate=5.0, rank=None),
            key_points=("AI chip demand surges",),
            mentioned_with=(
                CoMention(name="OpenAI", type="company", shared_article_count=4),
            ),
        )
        bundle = TrendsBundle(
            weeks=TrendWeeks(snapshot_date=date(2026, 5, 3)),
            category_trends=(
                CategoryTrends(
                    category_slug="ai", category_name="AI", mention_trends=(trend,)
                ),
            ),
        )
        resp = trends_from_snapshot(
            bundle=bundle,
            generated_at=datetime(2026, 5, 3, 0, 5, tzinfo=UTC),
            analyzed_article_count=328,
        )
        assert resp.model_dump(mode="json", by_alias=True) == {
            "week": {"start": "2026-04-26", "end": "2026-05-02"},
            "previousWeek": {"start": "2026-04-19", "end": "2026-04-25"},
            "generatedAt": "2026-05-03T00:05:00Z",
            "analyzedArticleCount": 328,
            "categoryTrends": [
                {
                    "category": {"slug": "ai", "name": "AI"},
                    "mentionTrends": [
                        {
                            "name": "NVIDIA",
                            "type": "company",
                            "articleVolume": {
                                "count": 30,
                                "previousWeekCount": 5,
                                "rank": 1,
                            },
                            "growth": {"rate": 5.0, "rank": None},
                            "keyPoints": ["AI chip demand surges"],
                            "mentionedWith": [
                                {
                                    "name": "OpenAI",
                                    "type": "company",
                                    "sharedArticleCount": 4,
                                }
                            ],
                        }
                    ],
                }
            ],
        }

    def test_week_end_is_the_last_day_before_the_snapshot_date(self) -> None:
        """週の end は最終日で、スナップショットの日付 (翌日) ではない。"""
        bundle = TrendsBundle(
            weeks=TrendWeeks(snapshot_date=date(2026, 4, 30)), category_trends=()
        )
        resp = trends_from_snapshot(
            bundle=bundle,
            generated_at=datetime(2026, 4, 30, 0, 5, tzinfo=UTC),
            analyzed_article_count=0,
        )
        dumped = resp.model_dump(mode="json", by_alias=True)
        assert (dumped["week"], dumped["previousWeek"]) == (
            {"start": "2026-04-23", "end": "2026-04-29"},
            {"start": "2026-04-16", "end": "2026-04-22"},
        )
