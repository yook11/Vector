"""GET /api/v1/weekly-trends ルーターの E2E テスト。

検証する観点:
- snapshot 不在時は 200 + state="empty" のみ (failure_visibility 原則:
  500 にはせず空状態を discriminated union で表現する)
- snapshot 在ると最新窓の bundle が state="ready" + camelCase で返る
- 複数 window_end がある場合は window_end DESC で 1 件目を返す
- 認証は任意 (BFF プロキシヘッダなしでも 200)
- bundle JSONB が破損していて Pydantic validate に失敗したらルーターは
  捕まえずに伝播させる (生成側の不具合を fallback で隠さない:
  feedback_failure_visibility)。本番では FastAPI が 500 に変換する
"""

from __future__ import annotations

from datetime import date

import pytest
from httpx import AsyncClient
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.insights.snapshot.domain.trend import (
    MAX_CATEGORIES_PER_BUNDLE,
    MAX_TRENDS_PER_CATEGORY,
    EntityTrend,
    NewEntity,
    TopicTrend,
    WeeklyCategoryTrends,
    WeeklyTrendsBundle,
)
from app.models.weekly_trends_snapshot import WeeklyTrendsSnapshot


def _bundle_with_section(window_end: date) -> WeeklyTrendsBundle:
    section = WeeklyCategoryTrends(
        category_id=1,
        category_slug="ai",
        category_name="AI",
        trending_entities=(
            EntityTrend(
                name="NVIDIA", type="company", current_count=30, previous_count=5
            ),
        ),
        trending_topics=(
            TopicTrend(topic="ai agents", current_count=12, previous_count=1),
        ),
        new_entities=(NewEntity(name="Acme", type="company", current_count=3),),
    )
    return WeeklyTrendsBundle(window_end=window_end, sections=(section,))


def _snapshot(window_end: date, *, bundle: dict | None = None) -> WeeklyTrendsSnapshot:
    serialized = (
        bundle
        if bundle is not None
        else _bundle_with_section(window_end).model_dump(mode="json")
    )
    return WeeklyTrendsSnapshot(
        window_end=window_end,
        bundle=serialized,
        source_analysis_count=42,
    )


@pytest.mark.asyncio
class TestWeeklyTrendsEndpoint:
    async def test_empty_state_returns_200_with_state_empty(
        self, client: AsyncClient
    ) -> None:
        resp = await client.get("/api/v1/weekly-trends")
        assert resp.status_code == 200
        assert resp.json() == {"state": "empty"}

    async def test_returns_latest_snapshot(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
    ) -> None:
        db_session.add(_snapshot(date(2026, 5, 1)))
        db_session.add(_snapshot(date(2026, 5, 3)))
        await db_session.commit()

        resp = await client.get("/api/v1/weekly-trends")
        assert resp.status_code == 200
        data = resp.json()
        assert data["state"] == "ready"
        assert data["windowEnd"] == "2026-05-03"
        assert data["windowStart"] == "2026-04-26"
        assert data["sourceAnalysisCount"] == 42
        assert len(data["categories"]) == 1

        section = data["categories"][0]
        assert section["categorySlug"] == "ai"
        assert section["trendingEntities"][0]["name"] == "NVIDIA"
        assert section["trendingTopics"][0]["topic"] == "ai agents"
        assert section["newEntities"][0]["name"] == "Acme"

    async def test_no_auth_required(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
    ) -> None:
        db_session.add(_snapshot(date(2026, 5, 3)))
        await db_session.commit()

        resp = await client.get("/api/v1/weekly-trends")
        assert resp.status_code == 200

    async def test_corrupt_bundle_propagates_validation_error(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
    ) -> None:
        """bundle JSONB が schema 違反ならルーターは捕まえずに伝播させる。

        本番では FastAPI が unhandled exception を 500 に変換する。テストでは
        ASGITransport(raise_app_exceptions=True) のため ValidationError 自体を
        確認することで「ルーターが try/except で隠していない」ことを構造的に
        保証する (feedback_failure_visibility)。
        """
        db_session.add(_snapshot(date(2026, 5, 3), bundle={"window_end": "not-a-date"}))
        await db_session.commit()

        with pytest.raises(ValidationError):
            await client.get("/api/v1/weekly-trends")

    async def test_anon_get_rejects_oversized_trends_in_section(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
    ) -> None:
        """1 section 内の trending_entities が上限超なら anon GET で 500 (DoS 遮断)。

        AUTH-N4 / AUTH-C1 経由で attacker が DB に巨大 JSONB を直書きしたシナリオ。
        domain VO の Field(max_length=MAX_TRENDS_PER_CATEGORY) が router の
        WeeklyTrendsBundle.model_validate(snapshot.bundle) で発火し、巨大 response
        が anon に流れることを構造的に防ぐ (red-team F10)。
        """
        oversized_entities = [
            {
                "name": f"ent_{i:03d}",
                "type": "company",
                "current_count": 5,
                "previous_count": 0,
            }
            for i in range(MAX_TRENDS_PER_CATEGORY + 1)
        ]
        bundle = {
            "window_end": "2026-05-03",
            "sections": [
                {
                    "category_id": 1,
                    "category_slug": "ai",
                    "category_name": "AI",
                    "trending_entities": oversized_entities,
                    "trending_topics": [],
                    "new_entities": [],
                }
            ],
        }
        db_session.add(_snapshot(date(2026, 5, 3), bundle=bundle))
        await db_session.commit()

        with pytest.raises(ValidationError):
            await client.get("/api/v1/weekly-trends")

    async def test_anon_get_rejects_too_many_sections(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
    ) -> None:
        """sections が上限超なら anon GET で 500 (DoS 遮断、F10 sections 軸)。"""
        oversized_sections = [
            {
                "category_id": i + 1,
                "category_slug": f"slug_{i:02d}",
                "category_name": f"Cat {i}",
                "trending_entities": [],
                "trending_topics": [],
                "new_entities": [],
            }
            for i in range(MAX_CATEGORIES_PER_BUNDLE + 1)
        ]
        bundle = {"window_end": "2026-05-03", "sections": oversized_sections}
        db_session.add(_snapshot(date(2026, 5, 3), bundle=bundle))
        await db_session.commit()

        with pytest.raises(ValidationError):
            await client.get("/api/v1/weekly-trends")
