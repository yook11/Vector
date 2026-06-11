"""SnapshotRepository の永続化挙動テスト。

検証する観点:
- ``find_latest`` / ``find_by_window_end`` の基本挙動
- ``exists_for_window_end``: 不在 / 存在の cheap 判定
- ``save(force=False)``: 新規で INSERTED / 衝突で CONFLICT (副作用なし)
- ``save(force=True)``: 新規で INSERTED / 既存で UPDATED
- 並行 save (asyncio.gather): 1 つは INSERTED / 1 つは CONFLICT
"""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.insights.trend_discovery.repository import (
    SnapshotRepository,
    SnapshotSaveResult,
    SnapshotSaveStatus,
)
from app.models.trends_snapshot import TrendsSnapshot

# generated_at は server_default を持たず呼び出し側が常に供給する (アプリが時計の源)。
_GENERATED_AT = datetime(2026, 5, 3, tzinfo=UTC)


def _snapshot(
    window_end: date,
    *,
    source_analysis_count: int = 10,
    marker: str = "v1",
    generated_at: datetime = _GENERATED_AT,
) -> TrendsSnapshot:
    return TrendsSnapshot(
        window_end=window_end,
        bundle={
            "window_end": window_end.isoformat(),
            "marker": marker,
            "category_trends": [],
        },
        source_analysis_count=source_analysis_count,
        generated_at=generated_at,
    )


class TestFindLatest:
    @pytest.mark.asyncio
    async def test_returns_none_when_empty(self, db_session: AsyncSession) -> None:
        repo = SnapshotRepository(db_session)
        assert await repo.find_latest() is None

    @pytest.mark.asyncio
    async def test_returns_most_recent_window_end(
        self, db_session: AsyncSession
    ) -> None:
        repo = SnapshotRepository(db_session)
        for offset in (0, 1, 2):
            snap = _snapshot(date(2026, 5, 3) - timedelta(days=offset))
            await repo.save(snap)
        await db_session.commit()

        latest = await repo.find_latest()
        assert latest is not None
        assert latest.window_end == date(2026, 5, 3)


class TestFindByWindowEnd:
    @pytest.mark.asyncio
    async def test_returns_snapshot_when_present(
        self, db_session: AsyncSession
    ) -> None:
        repo = SnapshotRepository(db_session)
        await repo.save(_snapshot(date(2026, 5, 3)))
        await db_session.commit()

        found = await repo.find_by_window_end(date(2026, 5, 3))
        assert found is not None
        assert found.window_end == date(2026, 5, 3)

    @pytest.mark.asyncio
    async def test_returns_none_when_missing(self, db_session: AsyncSession) -> None:
        repo = SnapshotRepository(db_session)
        assert await repo.find_by_window_end(date(2026, 5, 3)) is None


class TestExistsForWindowEnd:
    @pytest.mark.asyncio
    async def test_returns_false_when_missing(self, db_session: AsyncSession) -> None:
        repo = SnapshotRepository(db_session)
        assert await repo.exists_for_window_end(date(2026, 5, 3)) is False

    @pytest.mark.asyncio
    async def test_returns_true_after_save(self, db_session: AsyncSession) -> None:
        repo = SnapshotRepository(db_session)
        await repo.save(_snapshot(date(2026, 5, 3)))
        await db_session.commit()
        assert await repo.exists_for_window_end(date(2026, 5, 3)) is True

    @pytest.mark.asyncio
    async def test_returns_false_for_other_date(self, db_session: AsyncSession) -> None:
        repo = SnapshotRepository(db_session)
        await repo.save(_snapshot(date(2026, 5, 3)))
        await db_session.commit()
        assert await repo.exists_for_window_end(date(2026, 5, 2)) is False


# save (force=False)


class TestSaveDefault:
    @pytest.mark.asyncio
    async def test_returns_snapshot_on_new_insert(
        self, db_session: AsyncSession
    ) -> None:
        repo = SnapshotRepository(db_session)
        result = await repo.save(_snapshot(date(2026, 5, 3)))
        assert result.status == SnapshotSaveStatus.INSERTED
        assert result.snapshot is not None
        assert result.snapshot.window_end == date(2026, 5, 3)
        assert result.snapshot.generated_at is not None

    @pytest.mark.asyncio
    async def test_returns_conflict_on_conflict(self, db_session: AsyncSession) -> None:
        repo = SnapshotRepository(db_session)
        first = await repo.save(_snapshot(date(2026, 5, 3), marker="first"))
        await db_session.commit()
        assert first.status == SnapshotSaveStatus.INSERTED

        second = await repo.save(_snapshot(date(2026, 5, 3), marker="second"))
        assert second.status == SnapshotSaveStatus.CONFLICT
        assert second.snapshot is None

    @pytest.mark.asyncio
    async def test_conflict_does_not_overwrite(self, db_session: AsyncSession) -> None:
        """``save(force=False)`` 衝突時、既存行は更新されない。"""
        repo = SnapshotRepository(db_session)
        await repo.save(
            _snapshot(date(2026, 5, 3), source_analysis_count=10, marker="first")
        )
        await db_session.commit()

        await repo.save(
            _snapshot(date(2026, 5, 3), source_analysis_count=99, marker="second")
        )
        await db_session.commit()

        existing = await repo.find_by_window_end(date(2026, 5, 3))
        assert existing is not None
        assert existing.source_analysis_count == 10
        assert existing.bundle["marker"] == "first"


# save (force=True) — UPSERT 経路


class TestSaveForce:
    @pytest.mark.asyncio
    async def test_inserts_when_absent(self, db_session: AsyncSession) -> None:
        repo = SnapshotRepository(db_session)
        result = await repo.save(_snapshot(date(2026, 5, 3)), force=True)
        await db_session.commit()
        assert result.status == SnapshotSaveStatus.INSERTED
        assert result.snapshot is not None

        existing = await repo.find_by_window_end(date(2026, 5, 3))
        assert existing is not None
        assert existing.bundle["marker"] == "v1"

    @pytest.mark.asyncio
    async def test_overwrites_existing(self, db_session: AsyncSession) -> None:
        repo = SnapshotRepository(db_session)
        first_generated_at = _GENERATED_AT
        first = await repo.save(
            _snapshot(
                date(2026, 5, 3),
                source_analysis_count=10,
                marker="first",
                generated_at=first_generated_at,
            )
        )
        await db_session.commit()
        assert first.snapshot is not None

        second_generated_at = first_generated_at + timedelta(hours=1)
        second = await repo.save(
            _snapshot(
                date(2026, 5, 3),
                source_analysis_count=99,
                marker="second",
                generated_at=second_generated_at,
            ),
            force=True,
        )
        await db_session.commit()
        assert second.status == SnapshotSaveStatus.UPDATED
        assert second.snapshot is not None
        # force=True は呼び出し側が確定した generated_at で上書きする
        assert second.snapshot.generated_at == second_generated_at
        assert second.snapshot.generated_at != first_generated_at

        existing = await repo.find_by_window_end(date(2026, 5, 3))
        assert existing is not None
        assert existing.source_analysis_count == 99
        assert existing.bundle["marker"] == "second"


# 並行 save 統合テスト (Phase 1-3 同型)


class TestConcurrentSave:
    @pytest.mark.asyncio
    async def test_concurrent_save_returns_one_inserted_one_conflict(
        self,
        db_session: AsyncSession,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """同一 window_end への並行 save は片方が CONFLICT になる。"""
        target_window_end = date(2026, 5, 3)

        async def _save_in_new_session() -> SnapshotSaveResult:
            async with session_factory() as session:
                repo = SnapshotRepository(session)
                result = await repo.save(_snapshot(target_window_end))
                await session.commit()
                return result

        results = await asyncio.gather(
            _save_in_new_session(),
            _save_in_new_session(),
        )
        statuses = [r.status for r in results]
        assert statuses.count(SnapshotSaveStatus.INSERTED) == 1
        assert statuses.count(SnapshotSaveStatus.CONFLICT) == 1

        # 永続化された snapshot は 1 件のみ
        async with session_factory() as session:
            repo = SnapshotRepository(session)
            assert await repo.exists_for_window_end(target_window_end) is True
