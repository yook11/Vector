"""WeeklyTrendsQueryService — find_latest が SnapshotRepository に委譲する。

Read 経路の Service は薄いが、Router からの入口を 1 箇所に集約する責務を持つ
(Phase 1A 確定設計の CQRS 風分離: Command = Snapshot Service / Query = この
Service)。
"""

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.insights.snapshot.application.query import WeeklyTrendsQueryService
from app.insights.snapshot.domain.trend import WeeklyTrendsBundle
from app.models.weekly_trends_snapshot import WeeklyTrendsSnapshot


def _snapshot(
    window_end: date, *, source_analysis_count: int = 10
) -> WeeklyTrendsSnapshot:
    bundle = WeeklyTrendsBundle(window_end=window_end, sections=())
    return WeeklyTrendsSnapshot(
        window_end=window_end,
        bundle=bundle.model_dump(mode="json"),
        source_analysis_count=source_analysis_count,
    )


@pytest.mark.asyncio
class TestFindLatest:
    async def test_returns_none_when_empty(self, db_session: AsyncSession) -> None:
        service = WeeklyTrendsQueryService(db_session)
        assert await service.find_latest() is None

    async def test_returns_latest_by_window_end_desc(
        self, db_session: AsyncSession
    ) -> None:
        db_session.add(_snapshot(date(2026, 4, 30)))
        db_session.add(_snapshot(date(2026, 5, 3)))
        db_session.add(_snapshot(date(2026, 5, 1)))
        await db_session.flush()

        service = WeeklyTrendsQueryService(db_session)
        latest = await service.find_latest()

        assert latest is not None
        assert latest.window_end == date(2026, 5, 3)
