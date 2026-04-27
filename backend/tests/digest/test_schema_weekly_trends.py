"""WeeklyTrendsResponse schema の期待動作。

API レスポンスの keys は camelCase に揃える (Vector 全体規約)。snapshot 不在時
の「空状態」を nullable + 空 list で表現できることをテスト境界で固定する。
"""

from __future__ import annotations

from datetime import UTC, date, datetime

from app.digest.domain.trend import (
    EntityTrend,
    NewEntity,
    TopicTrend,
    WeeklyCategoryTrends,
    WeeklyTrendsBundle,
)
from app.digest.schemas.weekly_trends import WeeklyTrendsResponse


class TestEmptyState:
    def test_absent_snapshot_serializes_with_nulls(self) -> None:
        """snapshot 不在時は週情報が null + categories が空 list。"""
        resp = WeeklyTrendsResponse.empty()
        dumped = resp.model_dump(mode="json", by_alias=True)
        assert dumped["weekStart"] is None
        assert dumped["weekEnd"] is None
        assert dumped["generatedAt"] is None
        assert dumped["sourceAnalysisCount"] is None
        assert dumped["categories"] == []


class TestFromSnapshot:
    def _bundle(self) -> WeeklyTrendsBundle:
        section = WeeklyCategoryTrends(
            category_id=1,
            category_slug="ai",
            category_name="AI",
            trending_entities=(
                EntityTrend(
                    name="NVIDIA",
                    type="company",
                    current_count=30,
                    previous_count=5,
                ),
            ),
            trending_topics=(
                TopicTrend(topic="ai agents", current_count=12, previous_count=1),
            ),
            new_entities=(NewEntity(name="Acme", type="company", current_count=3),),
        )
        return WeeklyTrendsBundle(week_start=date(2026, 4, 20), sections=(section,))

    def test_camel_case_keys(self) -> None:
        bundle = self._bundle()
        resp = WeeklyTrendsResponse.from_snapshot(
            bundle=bundle,
            generated_at=datetime(2026, 4, 21, 0, 5, tzinfo=UTC),
            source_analysis_count=328,
        )
        dumped = resp.model_dump(mode="json", by_alias=True)
        assert dumped["weekStart"] == "2026-04-20"
        assert dumped["weekEnd"] == "2026-04-27"
        assert dumped["generatedAt"] is not None
        assert dumped["sourceAnalysisCount"] == 328

        section = dumped["categories"][0]
        assert section["categoryId"] == 1
        assert section["categorySlug"] == "ai"
        assert section["categoryName"] == "AI"

        entity = section["trendingEntities"][0]
        assert entity["name"] == "NVIDIA"
        assert entity["type"] == "company"
        assert entity["currentCount"] == 30
        assert entity["previousCount"] == 5
        assert entity["hotnessScore"] == 5.0  # (30-5)/max(5,2)

        topic = section["trendingTopics"][0]
        assert topic["topic"] == "ai agents"
        assert topic["currentCount"] == 12
        assert topic["hotnessScore"] == 5.5  # (12-1)/max(1,2)

        new_e = section["newEntities"][0]
        assert new_e["name"] == "Acme"
        assert new_e["currentCount"] == 3

    def test_week_end_is_week_start_plus_seven_days(self) -> None:
        bundle = WeeklyTrendsBundle(week_start=date(2026, 4, 13), sections=())
        resp = WeeklyTrendsResponse.from_snapshot(
            bundle=bundle,
            generated_at=datetime(2026, 4, 14, 0, 5, tzinfo=UTC),
            source_analysis_count=0,
        )
        dumped = resp.model_dump(mode="json", by_alias=True)
        assert dumped["weekStart"] == "2026-04-13"
        assert dumped["weekEnd"] == "2026-04-20"
