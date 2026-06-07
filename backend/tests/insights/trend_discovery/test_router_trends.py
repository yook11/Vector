"""GET /api/v1/trends ルーターの E2E テスト。

検証する観点:
- snapshot 不在時は 200 + state="empty" のみ (failure_visibility 原則:
  500 にはせず空状態を discriminated union で表現する)
- snapshot 在ると最新窓の bundle が state="trends" + camelCase で返る
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

from app.insights.trend_discovery.domain.trend import (
    MAX_CATEGORIES_PER_BUNDLE,
    TOP_N_PER_RANKING,
    CategoryTrends,
    RankedMention,
    TrendsBundle,
)
from app.models.trends_snapshot import TrendsSnapshot


def _bundle_with_category_trends(window_end: date) -> TrendsBundle:
    mention = RankedMention(
        name="NVIDIA", type="company", appearance_count=30, previous_appearance_count=5
    )
    category_trends = CategoryTrends(
        category_id=1,
        category_slug="ai",
        category_name="AI",
        most_mentioned=(mention,),
        fastest_growing=(mention,),
    )
    return TrendsBundle(window_end=window_end, category_trends=(category_trends,))


def _snapshot(window_end: date, *, bundle: dict | None = None) -> TrendsSnapshot:
    serialized = (
        bundle
        if bundle is not None
        else _bundle_with_category_trends(window_end).model_dump(mode="json")
    )
    return TrendsSnapshot(
        window_end=window_end,
        bundle=serialized,
        source_analysis_count=42,
    )


@pytest.mark.asyncio
class TestTrendsEndpoint:
    async def test_empty_state_returns_200_with_state_empty(
        self, client: AsyncClient
    ) -> None:
        resp = await client.get("/api/v1/trends")
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

        resp = await client.get("/api/v1/trends")
        assert resp.status_code == 200
        data = resp.json()
        assert data["state"] == "trends"
        assert data["windowEnd"] == "2026-05-03"
        assert data["windowStart"] == "2026-04-26"
        assert data["sourceAnalysisCount"] == 42
        assert len(data["categoryTrends"]) == 1

        category_trends = data["categoryTrends"][0]
        assert category_trends["categorySlug"] == "ai"
        assert category_trends["mostMentioned"][0]["name"] == "NVIDIA"
        assert category_trends["fastestGrowing"][0]["name"] == "NVIDIA"

    async def test_no_auth_required(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
    ) -> None:
        db_session.add(_snapshot(date(2026, 5, 3)))
        await db_session.commit()

        resp = await client.get("/api/v1/trends")
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
            await client.get("/api/v1/trends")

    async def test_anon_get_rejects_oversized_ranking_in_category_trends(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
    ) -> None:
        """1 カテゴリ分の most_mentioned が上限超なら anon GET で 500 (DoS 遮断)。

        AUTH-N4 / AUTH-C1 経由で attacker が DB に巨大 JSONB を直書きしたシナリオ。
        domain VO の Field(max_length=TOP_N_PER_RANKING) が router の
        TrendsBundle.model_validate(snapshot.bundle) で発火し、巨大 response
        が anon に流れることを構造的に防ぐ (red-team F10)。
        """
        oversized_mentions = [
            {
                "name": f"ent_{i:03d}",
                "type": "company",
                "appearance_count": 5,
                "previous_appearance_count": 0,
                "key_points": [],
                "related_mentions": [],
            }
            for i in range(TOP_N_PER_RANKING + 1)
        ]
        bundle = {
            "window_end": "2026-05-03",
            "category_trends": [
                {
                    "category_id": 1,
                    "category_slug": "ai",
                    "category_name": "AI",
                    "most_mentioned": oversized_mentions,
                    "fastest_growing": [],
                }
            ],
        }
        db_session.add(_snapshot(date(2026, 5, 3), bundle=bundle))
        await db_session.commit()

        with pytest.raises(ValidationError):
            await client.get("/api/v1/trends")

    async def test_anon_get_rejects_too_many_category_trends(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
    ) -> None:
        """category_trends が上限超なら anon GET で 500 (DoS 遮断、F10 カテゴリ軸)。"""
        oversized_category_trends = [
            {
                "category_id": i + 1,
                "category_slug": f"slug_{i:02d}",
                "category_name": f"Cat {i}",
                "most_mentioned": [],
                "fastest_growing": [],
            }
            for i in range(MAX_CATEGORIES_PER_BUNDLE + 1)
        ]
        bundle = {
            "window_end": "2026-05-03",
            "category_trends": oversized_category_trends,
        }
        db_session.add(_snapshot(date(2026, 5, 3), bundle=bundle))
        await db_session.commit()

        with pytest.raises(ValidationError):
            await client.get("/api/v1/trends")
