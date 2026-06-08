"""GET /api/v1/trends ルーターの E2E テスト。

検証する観点:
- snapshot 不在時は 200 + state="empty" のみ (failure_visibility 原則:
  500 にはせず空状態を discriminated union で表現する)
- snapshot 在ると最新窓の bundle が verbatim (値等価) で返る
- 複数 window_end がある場合は window_end DESC で 1 件目を返す
- 認証は任意 (BFF プロキシヘッダなしでも 200)
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.insights.trend_discovery.domain.trend import (
    CategoryTrends,
    RankedMention,
    TrendsBundle,
)
from app.insights.trend_discovery.schemas.trends import trends_from_snapshot
from app.models.trends_snapshot import TrendsSnapshot


def _camel_bundle(window_end: date) -> dict:
    """camelCase API payload を生成する (service と同じ経路)。"""
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
    bundle = TrendsBundle(window_end=window_end, category_trends=(category_trends,))
    generated_at = datetime(2026, 5, 3, 0, 0, 0, tzinfo=UTC)
    response = trends_from_snapshot(
        bundle=bundle, generated_at=generated_at, source_analysis_count=42
    )
    return response.model_dump(mode="json", by_alias=True)


def _snapshot(window_end: date, *, bundle: dict | None = None) -> TrendsSnapshot:
    """TrendsSnapshot を camelCase payload で組み立てる。"""
    payload = bundle if bundle is not None else _camel_bundle(window_end)
    # generated_at は payload と列で同値にする
    generated_at = datetime(2026, 5, 3, 0, 0, 0, tzinfo=UTC)
    return TrendsSnapshot(
        window_end=window_end,
        bundle=payload,
        source_analysis_count=42,
        generated_at=generated_at,
    )


@pytest.mark.asyncio
class TestTrendsEndpoint:
    async def test_empty_state_returns_200_with_state_empty(
        self, client: AsyncClient
    ) -> None:
        resp = await client.get("/api/v1/trends")
        assert resp.status_code == 200
        assert resp.json() == {"state": "empty"}

    async def test_returns_snapshot_bundle_verbatim(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
    ) -> None:
        """snapshot.bundle がそのまま (値等価で) レスポンスとして返る。

        model_validate や trends_from_snapshot の介在がないことの証明は
        値等価であることで十分 (読取時の変換があれば値がズレる)。
        """
        snap = _snapshot(date(2026, 5, 3))
        db_session.add(snap)
        await db_session.commit()

        resp = await client.get("/api/v1/trends")
        assert resp.status_code == 200
        assert resp.json() == snap.bundle

    async def test_returns_latest_by_window_end(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
    ) -> None:
        """複数 snapshot がある場合は window_end DESC で最新 1 件を返す。"""
        db_session.add(_snapshot(date(2026, 5, 1)))
        db_session.add(_snapshot(date(2026, 5, 3)))
        await db_session.commit()

        resp = await client.get("/api/v1/trends")
        assert resp.status_code == 200
        data = resp.json()
        assert data["state"] == "trends"
        assert data["windowEnd"] == "2026-05-03"

    async def test_no_auth_required(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
    ) -> None:
        db_session.add(_snapshot(date(2026, 5, 3)))
        await db_session.commit()

        resp = await client.get("/api/v1/trends")
        assert resp.status_code == 200
