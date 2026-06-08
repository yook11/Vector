"""TrendsQueryService — find_latest が SnapshotRepository に委譲する。

Read 経路の Service は薄いが、Router からの入口を 1 箇所に集約する責務を持つ
(Phase 1A 確定設計の CQRS 風分離: Command = TrendDiscoveryService / Query =
この Service)。
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.insights.trend_discovery.application.query import TrendsQueryService
from app.insights.trend_discovery.domain.trend import TrendsBundle
from app.insights.trend_discovery.schemas.trends import trends_from_snapshot
from app.models.trends_snapshot import TrendsSnapshot


def _snapshot(window_end: date, *, source_analysis_count: int = 10) -> TrendsSnapshot:
    """camelCase API payload で snapshot を組み立てる (service と同じ経路)。"""
    bundle = TrendsBundle(window_end=window_end, category_trends=())
    generated_at = datetime(2026, 5, 3, 0, 0, 0, tzinfo=UTC)
    payload = trends_from_snapshot(
        bundle=bundle,
        generated_at=generated_at,
        source_analysis_count=source_analysis_count,
    ).model_dump(mode="json", by_alias=True)
    return TrendsSnapshot(
        window_end=window_end,
        bundle=payload,
        source_analysis_count=source_analysis_count,
        generated_at=generated_at,
    )


@pytest.mark.asyncio
class TestFindLatest:
    async def test_returns_none_when_empty(self, db_session: AsyncSession) -> None:
        service = TrendsQueryService(db_session)
        assert await service.find_latest() is None

    async def test_returns_latest_by_window_end_desc(
        self, db_session: AsyncSession
    ) -> None:
        db_session.add(_snapshot(date(2026, 4, 30)))
        db_session.add(_snapshot(date(2026, 5, 3)))
        db_session.add(_snapshot(date(2026, 5, 1)))
        await db_session.flush()

        service = TrendsQueryService(db_session)
        latest = await service.find_latest()

        assert latest is not None
        assert latest.window_end == date(2026, 5, 3)
