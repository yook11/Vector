"""SnapshotRepository の永続化挙動テスト。

検証する観点:
- ``find_latest`` / ``find_by_week`` の基本挙動
- ``exists_for_week``: 不在 / 存在の cheap 判定
- ``save(force=False)``: 新規で Snapshot 返却 / 衝突で None (副作用なし)
- ``save(force=True)``: 新規で Snapshot 返却 / 既存で上書き ``generated_at`` 更新
- 並行 save (asyncio.gather): 1 つは Snapshot / 1 つは None (Phase 1-3 同型)
"""

from __future__ import annotations

import asyncio
from datetime import date, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.insights.snapshot.repository.snapshots import SnapshotRepository
from app.models.weekly_trends_snapshot import WeeklyTrendsSnapshot


def _snapshot(
    week_start: date, *, source_analysis_count: int = 10, marker: str = "v1"
) -> WeeklyTrendsSnapshot:
    return WeeklyTrendsSnapshot(
        week_start=week_start,
        bundle={"week_start": week_start.isoformat(), "marker": marker, "sections": []},
        source_analysis_count=source_analysis_count,
    )


# ---------------------------------------------------------------------------
# find_latest / find_by_week
# ---------------------------------------------------------------------------


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
            await repo.save(snap)
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
        await repo.save(_snapshot(date(2026, 4, 13)))
        await db_session.commit()

        found = await repo.find_by_week(date(2026, 4, 13))
        assert found is not None
        assert found.week_start == date(2026, 4, 13)

    @pytest.mark.asyncio
    async def test_returns_none_when_missing(self, db_session: AsyncSession) -> None:
        repo = SnapshotRepository(db_session)
        assert await repo.find_by_week(date(2026, 4, 13)) is None


# ---------------------------------------------------------------------------
# exists_for_week
# ---------------------------------------------------------------------------


class TestExistsForWeek:
    @pytest.mark.asyncio
    async def test_returns_false_when_missing(self, db_session: AsyncSession) -> None:
        repo = SnapshotRepository(db_session)
        assert await repo.exists_for_week(date(2026, 4, 13)) is False

    @pytest.mark.asyncio
    async def test_returns_true_after_save(self, db_session: AsyncSession) -> None:
        repo = SnapshotRepository(db_session)
        await repo.save(_snapshot(date(2026, 4, 13)))
        await db_session.commit()
        assert await repo.exists_for_week(date(2026, 4, 13)) is True

    @pytest.mark.asyncio
    async def test_returns_false_for_other_week(self, db_session: AsyncSession) -> None:
        repo = SnapshotRepository(db_session)
        await repo.save(_snapshot(date(2026, 4, 13)))
        await db_session.commit()
        assert await repo.exists_for_week(date(2026, 4, 20)) is False


# ---------------------------------------------------------------------------
# save (force=False)
# ---------------------------------------------------------------------------


class TestSaveDefault:
    @pytest.mark.asyncio
    async def test_returns_snapshot_on_new_insert(
        self, db_session: AsyncSession
    ) -> None:
        repo = SnapshotRepository(db_session)
        saved = await repo.save(_snapshot(date(2026, 4, 13)))
        assert saved is not None
        assert saved.week_start == date(2026, 4, 13)
        assert saved.generated_at is not None

    @pytest.mark.asyncio
    async def test_returns_none_on_conflict(self, db_session: AsyncSession) -> None:
        repo = SnapshotRepository(db_session)
        first = await repo.save(_snapshot(date(2026, 4, 13), marker="first"))
        await db_session.commit()
        assert first is not None

        second = await repo.save(_snapshot(date(2026, 4, 13), marker="second"))
        assert second is None

    @pytest.mark.asyncio
    async def test_conflict_does_not_overwrite(self, db_session: AsyncSession) -> None:
        """``save(force=False)`` 衝突時、既存行は更新されない。"""
        repo = SnapshotRepository(db_session)
        await repo.save(
            _snapshot(date(2026, 4, 13), source_analysis_count=10, marker="first")
        )
        await db_session.commit()

        await repo.save(
            _snapshot(date(2026, 4, 13), source_analysis_count=99, marker="second")
        )
        await db_session.commit()

        existing = await repo.find_by_week(date(2026, 4, 13))
        assert existing is not None
        assert existing.source_analysis_count == 10
        assert existing.bundle["marker"] == "first"


# ---------------------------------------------------------------------------
# save (force=True) — UPSERT 経路
# ---------------------------------------------------------------------------


class TestSaveForce:
    @pytest.mark.asyncio
    async def test_inserts_when_absent(self, db_session: AsyncSession) -> None:
        repo = SnapshotRepository(db_session)
        saved = await repo.save(_snapshot(date(2026, 4, 13)), force=True)
        await db_session.commit()
        assert saved is not None

        existing = await repo.find_by_week(date(2026, 4, 13))
        assert existing is not None
        assert existing.bundle["marker"] == "v1"

    @pytest.mark.asyncio
    async def test_overwrites_existing(self, db_session: AsyncSession) -> None:
        repo = SnapshotRepository(db_session)
        first = await repo.save(
            _snapshot(date(2026, 4, 13), source_analysis_count=10, marker="first")
        )
        await db_session.commit()
        assert first is not None
        first_generated_at = first.generated_at

        second = await repo.save(
            _snapshot(date(2026, 4, 13), source_analysis_count=99, marker="second"),
            force=True,
        )
        await db_session.commit()
        assert second is not None
        # generated_at は force=True で func.now() による更新が入る
        assert second.generated_at >= first_generated_at

        existing = await repo.find_by_week(date(2026, 4, 13))
        assert existing is not None
        assert existing.source_analysis_count == 99
        assert existing.bundle["marker"] == "second"


# ---------------------------------------------------------------------------
# 並行 save 統合テスト (Phase 1-3 同型)
# ---------------------------------------------------------------------------


class TestConcurrentSave:
    @pytest.mark.asyncio
    async def test_concurrent_save_returns_one_persisted_one_none(
        self,
        db_session: AsyncSession,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """同一 week_start への並行 save は片方が None になる (ON CONFLICT 動作)。"""
        target_week = date(2026, 4, 13)

        async def _save_in_new_session() -> WeeklyTrendsSnapshot | None:
            async with session_factory() as session:
                repo = SnapshotRepository(session)
                saved = await repo.save(_snapshot(target_week))
                await session.commit()
                return saved

        results = await asyncio.gather(
            _save_in_new_session(),
            _save_in_new_session(),
        )
        assert sum(1 for r in results if r is not None) == 1
        assert sum(1 for r in results if r is None) == 1

        # 永続化された snapshot は 1 件のみ
        async with session_factory() as session:
            repo = SnapshotRepository(session)
            assert await repo.exists_for_week(target_week) is True
