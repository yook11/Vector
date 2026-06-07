"""TrendsResponse schema の期待動作。

API レスポンスの keys は camelCase に揃える (Vector 全体規約)。snapshot 不在時の
「空状態」と生成済の「trends 状態」を ``state`` discriminator で構造的に分けて
表現できることをテスト境界で固定する。
"""

from __future__ import annotations

from datetime import UTC, date, datetime

from app.insights.trend_discovery.domain.trend import (
    CategoryRankings,
    RankedMention,
    RelatedMention,
    TrendsBundle,
)
from app.insights.trend_discovery.schemas.trends import (
    empty_trends,
    trends_from_snapshot,
)


class TestEmptyState:
    def test_absent_snapshot_serializes_with_state_empty(self) -> None:
        """snapshot 不在時は state="empty" のみで他フィールドは出力されない。"""
        resp = empty_trends()
        dumped = resp.model_dump(mode="json", by_alias=True)
        assert dumped == {"state": "empty"}


class TestFromSnapshot:
    def _bundle(self) -> TrendsBundle:
        mention = RankedMention(
            name="NVIDIA",
            type="company",
            appearance_count=30,
            previous_appearance_count=5,
            key_points=("AI chip demand surges",),
            related_mentions=(
                RelatedMention(name="OpenAI", type="company", shared_article_count=4),
            ),
        )
        section = CategoryRankings(
            category_id=1,
            category_slug="ai",
            category_name="AI",
            most_mentioned=(mention,),
            fastest_growing=(mention,),
        )
        return TrendsBundle(window_end=date(2026, 5, 3), sections=(section,))

    def test_camel_case_keys(self) -> None:
        bundle = self._bundle()
        resp = trends_from_snapshot(
            bundle=bundle,
            generated_at=datetime(2026, 5, 3, 0, 5, tzinfo=UTC),
            source_analysis_count=328,
        )
        dumped = resp.model_dump(mode="json", by_alias=True)
        assert dumped["state"] == "trends"
        assert dumped["windowEnd"] == "2026-05-03"
        assert dumped["windowStart"] == "2026-04-26"
        assert dumped["generatedAt"] is not None
        assert dumped["sourceAnalysisCount"] == 328

        section = dumped["categories"][0]
        assert section["categoryId"] == 1
        assert section["categorySlug"] == "ai"
        assert section["categoryName"] == "AI"

        mention = section["mostMentioned"][0]
        assert mention["name"] == "NVIDIA"
        assert mention["type"] == "company"
        assert mention["appearanceCount"] == 30
        assert mention["previousAppearanceCount"] == 5
        assert mention["growthRate"] == 5.0  # (30-5)/max(5,2)
        assert mention["keyPoints"] == ["AI chip demand surges"]

        related = mention["relatedMentions"][0]
        assert related["name"] == "OpenAI"
        assert related["type"] == "company"
        assert related["sharedArticleCount"] == 4

        # 同一 mention が両ランキングに載るケースを camelCase 構造で固定する。
        assert section["fastestGrowing"][0]["name"] == "NVIDIA"

    def test_window_start_is_window_end_minus_seven_days(self) -> None:
        bundle = TrendsBundle(window_end=date(2026, 4, 30), sections=())
        resp = trends_from_snapshot(
            bundle=bundle,
            generated_at=datetime(2026, 4, 30, 0, 5, tzinfo=UTC),
            source_analysis_count=0,
        )
        dumped = resp.model_dump(mode="json", by_alias=True)
        assert dumped["state"] == "trends"
        assert dumped["windowEnd"] == "2026-04-30"
        assert dumped["windowStart"] == "2026-04-23"
