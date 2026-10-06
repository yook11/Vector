"""GET /api/v1/trends ルーターの E2E テスト。

検証する観点:
- snapshot 不在時は 200 + null (failure_visibility 原則:
  まだ生成されていないことは故障ではないため 404 / 500 にしない)
- snapshot 在ると最新の bundle が現行 Trends schema で再検証され (round-trip で
  値等価のまま) 返る
- 旧 / 不完全 shape の bundle は ValidationError が伝播する (本番 500)
- 複数 snapshot がある場合はスナップショットの日付 (window_end 列) DESC で 1 件目を返す
- 認証は任意 (BFF プロキシヘッダなしでも 200)
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
from httpx2 import AsyncClient
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.insights.trend_discovery.domain.trend import (
    CategoryTrends,
    MentionArticleVolume,
    MentionGrowth,
    MentionTrend,
    TrendsBundle,
    TrendWeeks,
)
from app.insights.trend_discovery.schemas import trends_from_snapshot
from app.models.trends_snapshot import TrendsSnapshot


def _camel_bundle(snapshot_date: date) -> dict:
    """camelCase API payload を生成する (service と同じ経路)。"""
    trend = MentionTrend(
        name="NVIDIA",
        type="company",
        article_volume=MentionArticleVolume(count=30, previous_week_count=5, rank=1),
        growth=MentionGrowth(rate=5.0, rank=1),
    )
    category_trends = CategoryTrends(
        category_slug="ai", category_name="AI", mention_trends=(trend,)
    )
    bundle = TrendsBundle(
        weeks=TrendWeeks(snapshot_date=snapshot_date),
        category_trends=(category_trends,),
    )
    generated_at = datetime(2026, 5, 3, 0, 0, 0, tzinfo=UTC)
    response = trends_from_snapshot(
        bundle=bundle, generated_at=generated_at, analyzed_article_count=42
    )
    return response.model_dump(mode="json", by_alias=True)


def _snapshot(snapshot_date: date, *, bundle: dict | None = None) -> TrendsSnapshot:
    """TrendsSnapshot を camelCase payload で組み立てる。"""
    payload = bundle if bundle is not None else _camel_bundle(snapshot_date)
    # generated_at は payload と列で同値にする
    generated_at = datetime(2026, 5, 3, 0, 0, 0, tzinfo=UTC)
    return TrendsSnapshot(
        window_end=snapshot_date,
        bundle=payload,
        source_analysis_count=42,
        generated_at=generated_at,
    )


@pytest.mark.asyncio
class TestTrendsEndpoint:
    async def test_returns_null_when_no_snapshot_exists(
        self, bff_client: AsyncClient
    ) -> None:
        resp = await bff_client.get("/api/v1/trends")
        assert (resp.status_code, resp.json()) == (200, None)

    async def test_returns_validated_trends_round_trip(
        self,
        bff_client: AsyncClient,
        db_session: AsyncSession,
    ) -> None:
        """現行 schema に適合する bundle は再検証を通り、値等価のまま返る。

        読取時に ``Trends.model_validate`` を挟むが、生成時と同じ camelCase
        payload なので round-trip で値はズレない。
        """
        snap = _snapshot(date(2026, 5, 3))
        db_session.add(snap)
        await db_session.commit()

        resp = await bff_client.get("/api/v1/trends")
        assert resp.status_code == 200
        assert resp.json() == snap.bundle

    async def test_malformed_bundle_propagates_validation_error(
        self,
        bff_client: AsyncClient,
        db_session: AsyncSession,
    ) -> None:
        """旧 / 不完全 shape の bundle は ValidationError が伝播する (本番 500)。

        スキーマ進化を跨いだ旧行 (再設計前の sections 形など) を verbatim 配信
        すると frontend が crash するため、読取時に弾く。
        """
        legacy = _snapshot(
            date(2026, 5, 3),
            bundle={"windowEnd": "2026-05-03", "sections": [{"title": "x"}]},
        )
        db_session.add(legacy)
        await db_session.commit()

        with pytest.raises(ValidationError):
            await bff_client.get("/api/v1/trends")

    async def test_snapshot_saved_before_the_week_representation_is_rejected(
        self,
        bff_client: AsyncClient,
        db_session: AsyncSession,
    ) -> None:
        """順位を持たない2列の形で保存された行も、現行 schema に合わず 500 になる。"""
        db_session.add(
            _snapshot(
                date(2026, 5, 3),
                bundle={
                    "state": "trends",
                    "windowStart": "2026-04-26",
                    "windowEnd": "2026-05-03",
                    "generatedAt": "2026-05-03T00:00:00Z",
                    "sourceAnalysisCount": 42,
                    "categoryTrends": [
                        {
                            "categoryId": 1,
                            "categorySlug": "ai",
                            "categoryName": "AI",
                            "mostMentioned": [],
                            "fastestGrowing": [],
                        }
                    ],
                },
            )
        )
        await db_session.commit()

        with pytest.raises(ValidationError):
            await bff_client.get("/api/v1/trends")

    async def test_returns_latest_snapshot(
        self,
        bff_client: AsyncClient,
        db_session: AsyncSession,
    ) -> None:
        """複数 snapshot がある場合はスナップショットの日付が最も新しい1件を返す。"""
        db_session.add(_snapshot(date(2026, 5, 1)))
        db_session.add(_snapshot(date(2026, 5, 3)))
        await db_session.commit()

        resp = await bff_client.get("/api/v1/trends")
        assert resp.status_code == 200
        assert resp.json()["week"] == {"start": "2026-04-26", "end": "2026-05-02"}

    async def test_requires_bff_proof(self, client: AsyncClient) -> None:
        """BFF 経由証明の無い直叩きは 401 (login 検証ではなく BFF 経由証明)。"""
        resp = await client.get("/api/v1/trends")
        assert resp.status_code == 401
