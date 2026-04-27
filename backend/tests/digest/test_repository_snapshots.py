"""SnapshotRepository の永続化挙動テスト。

- ``find_latest``: 不在時 None / 最新 week_start を返す
- ``find_by_week``: PK lookup
- ``insert_if_absent``: 新規 INSERT で True / 衝突で False (副作用なし)
- ``upsert``: 新規 INSERT / 既存上書き (bundle, source_analysis_count, generated_at)
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.digest.repository.snapshots import SnapshotRepository
from app.models.weekly_trends_snapshot import WeeklyTrendsSnapshot


def _snapshot(
    week_start: date, *, source_analysis_count: int = 10, marker: str = "v1"
) -> WeeklyTrendsSnapshot:
    return WeeklyTrendsSnapshot(
        week_start=week_start,
        bundle={"week_start": week_start.isoformat(), "marker": marker, "sections": []},
        source_analysis_count=source_analysis_count,
    )


class TestFindLatest:
    @pytest.mark.asyncio
    async def test_returns_none_when_empty(self, db_session: AsyncSession) -> None:
        repo = SnapshotRepository(db_session)
        assert await repo.find_latest() is None

    @pytest.mark.asyncio
    async def test_returns_most_recent_week(self, db_session: AsyncSession) -> None:
        repo = SnapshotRepository(db_session)
        for offset in (0, 7, 14):
            snap = _snapshot(date(2026, 4, 13) - timedelta(days=offset))
            await repo.insert_if_absent(snap)
        await db_session.commit()

        latest = await repo.find_latest()
        assert latest is not None
        assert latest.week_start == date(2026, 4, 13)


class TestFindByWeek:
    @pytest.mark.asyncio
    async def test_returns_snapshot_when_present(
        self, db_session: AsyncSession
    ) -> None:
        repo = SnapshotRepository(db_session)
        await repo.insert_if_absent(_snapshot(date(2026, 4, 13)))
        await db_session.commit()

        found = await repo.find_by_week(date(2026, 4, 13))
        assert found is not None
        assert found.week_start == date(2026, 4, 13)

    @pytest.mark.asyncio
    async def test_returns_none_when_missing(self, db_session: AsyncSession) -> None:
        repo = SnapshotRepository(db_session)
        assert await repo.find_by_week(date(2026, 4, 13)) is None


class TestInsertIfAbsent:
    @pytest.mark.asyncio
    async def test_returns_true_on_new_insert(self, db_session: AsyncSession) -> None:
        repo = SnapshotRepository(db_session)
        inserted = await repo.insert_if_absent(_snapshot(date(2026, 4, 13)))
        assert inserted is True

    @pytest.mark.asyncio
    async def test_returns_false_on_conflict(self, db_session: AsyncSession) -> None:
        repo = SnapshotRepository(db_session)
        await repo.insert_if_absent(_snapshot(date(2026, 4, 13), marker="first"))
        await db_session.commit()

        retried = await repo.insert_if_absent(
            _snapshot(date(2026, 4, 13), marker="second")
        )
        assert retried is False

    @pytest.mark.asyncio
    async def test_conflict_does_not_overwrite(self, db_session: AsyncSession) -> None:
        """``insert_if_absent`` 衝突時、既存行は更新されない。"""
        repo = SnapshotRepository(db_session)
        await repo.insert_if_absent(
            _snapshot(date(2026, 4, 13), source_analysis_count=10, marker="first")
        )
        await db_session.commit()

        await repo.insert_if_absent(
            _snapshot(date(2026, 4, 13), source_analysis_count=99, marker="second")
        )
        await db_session.commit()

        existing = await repo.find_by_week(date(2026, 4, 13))
        assert existing is not None
        assert existing.source_analysis_count == 10
        assert existing.bundle["marker"] == "first"


class TestUpsert:
    @pytest.mark.asyncio
    async def test_inserts_when_absent(self, db_session: AsyncSession) -> None:
        repo = SnapshotRepository(db_session)
        await repo.upsert(_snapshot(date(2026, 4, 13)))
        await db_session.commit()

        existing = await repo.find_by_week(date(2026, 4, 13))
        assert existing is not None
        assert existing.bundle["marker"] == "v1"

    @pytest.mark.asyncio
    async def test_overwrites_existing(self, db_session: AsyncSession) -> None:
        repo = SnapshotRepository(db_session)
        await repo.insert_if_absent(
            _snapshot(date(2026, 4, 13), source_analysis_count=10, marker="first")
        )
        await db_session.commit()

        await repo.upsert(
            _snapshot(date(2026, 4, 13), source_analysis_count=99, marker="second")
        )
        await db_session.commit()

        existing = await repo.find_by_week(date(2026, 4, 13))
        assert existing is not None
        assert existing.source_analysis_count == 99
        assert existing.bundle["marker"] == "second"
