"""WeeklyTrendsResponse schema の期待動作。

API レスポンスの keys は camelCase に揃える (Vector 全体規約)。snapshot 不在時の
「空状態」と生成済の「ready 状態」を ``state`` discriminator で構造的に分けて
表現できることをテスト境界で固定する。
"""

from __future__ import annotations

from datetime import UTC, date, datetime

from app.insights.snapshot.domain.trend import (
    EntityTrend,
    NewEntity,
    WeeklyCategoryTrends,
    WeeklyTrendsBundle,
)
from app.insights.snapshot.schemas.weekly_trends import (
    empty_weekly_trends,
    weekly_trends_from_snapshot,
)


class TestEmptyState:
    def test_absent_snapshot_serializes_with_state_empty(self) -> None:
        """snapshot 不在時は state="empty" のみで他フィールドは出力されない。"""
        resp = empty_weekly_trends()
        dumped = resp.model_dump(mode="json", by_alias=True)
        assert dumped == {"state": "empty"}


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
            new_entities=(NewEntity(name="Acme", type="company", current_count=3),),
        )
        return WeeklyTrendsBundle(window_end=date(2026, 5, 3), sections=(section,))

    def test_camel_case_keys(self) -> None:
        bundle = self._bundle()
        resp = weekly_trends_from_snapshot(
            bundle=bundle,
            generated_at=datetime(2026, 5, 3, 0, 5, tzinfo=UTC),
            source_analysis_count=328,
        )
        dumped = resp.model_dump(mode="json", by_alias=True)
        assert dumped["state"] == "ready"
        assert dumped["windowEnd"] == "2026-05-03"
        assert dumped["windowStart"] == "2026-04-26"
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

        new_e = section["newEntities"][0]
        assert new_e["name"] == "Acme"
        assert new_e["currentCount"] == 3

    def test_window_start_is_window_end_minus_seven_days(self) -> None:
        bundle = WeeklyTrendsBundle(window_end=date(2026, 4, 30), sections=())
        resp = weekly_trends_from_snapshot(
            bundle=bundle,
            generated_at=datetime(2026, 4, 30, 0, 5, tzinfo=UTC),
            source_analysis_count=0,
        )
        dumped = resp.model_dump(mode="json", by_alias=True)
        assert dumped["state"] == "ready"
        assert dumped["windowEnd"] == "2026-04-30"
        assert dumped["windowStart"] == "2026-04-23"
