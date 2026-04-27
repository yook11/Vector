"""GET /api/v1/weekly-trends ルーターの E2E テスト。

検証する観点:
- snapshot 不在時は 200 + 全フィールド null + 空 categories (failure_visibility 原則:
  500 にはせず空状態を表現する。「生成されていない」は故障ではないため)
- snapshot 在ると最新週の bundle が camelCase で返る
- 複数週がある場合は week_start DESC で 1 件目を返す
- 認証は任意 (BFF プロキシヘッダなしでも 200)
- bundle JSONB が破損していて Pydantic validate に失敗したら 500 propagate
  (生成側の不具合を fallback で隠さない)
"""

from __future__ import annotations

from datetime import date

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.digest.domain.trend import (
    EntityTrend,
    NewEntity,
    TopicTrend,
    WeeklyCategoryTrends,
    WeeklyTrendsBundle,
)
from app.models.weekly_trends_snapshot import WeeklyTrendsSnapshot


def _bundle_with_section(week_start: date) -> WeeklyTrendsBundle:
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
    return WeeklyTrendsBundle(week_start=week_start, sections=(section,))


def _snapshot(week_start: date, *, bundle: dict | None = None) -> WeeklyTrendsSnapshot:
    serialized = bundle if bundle is not None else _bundle_with_section(week_start).model_dump(mode="json")
    return WeeklyTrendsSnapshot(
        week_start=week_start,
        bundle=serialized,
        source_analysis_count=42,
    )


@pytest.mark.asyncio
class TestWeeklyTrendsEndpoint:
    async def test_empty_state_returns_200_with_nulls(
        self, client: AsyncClient
    ) -> None:
        resp = await client.get("/api/v1/weekly-trends")
        assert resp.status_code == 200
        data = resp.json()
        assert data["weekStart"] is None
        assert data["weekEnd"] is None
        assert data["generatedAt"] is None
        assert data["sourceAnalysisCount"] is None
        assert data["categories"] == []

    async def test_returns_latest_snapshot(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
    ) -> None:
        db_session.add(_snapshot(date(2026, 4, 13)))
        db_session.add(_snapshot(date(2026, 4, 20)))
        await db_session.commit()

        resp = await client.get("/api/v1/weekly-trends")
        assert resp.status_code == 200
        data = resp.json()
        assert data["weekStart"] == "2026-04-20"
        assert data["weekEnd"] == "2026-04-27"
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
        db_session.add(_snapshot(date(2026, 4, 20)))
        await db_session.commit()

        resp = await client.get("/api/v1/weekly-trends")
        assert resp.status_code == 200

    async def test_corrupt_bundle_propagates_as_500(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
    ) -> None:
        """bundle JSONB が schema 違反なら 500 で表面化させる (failure_visibility)。"""
        db_session.add(
            _snapshot(date(2026, 4, 20), bundle={"week_start": "not-a-date"})
        )
        await db_session.commit()

        resp = await client.get("/api/v1/weekly-trends")
        assert resp.status_code == 500
