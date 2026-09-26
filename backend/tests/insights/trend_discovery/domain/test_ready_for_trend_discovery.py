"""``ReadyForTrendDiscovery`` の precondition / 不変条件テスト。

検証する観点:
- ``try_advance_from`` の precondition: exists_for_window_end
- frozen
- 任意の JST 日付 (月曜以外) を window_end として受け入れる
"""

from __future__ import annotations

from datetime import date

import pytest
from pydantic import ValidationError

from app.insights.trend_discovery.domain.ready import ReadyForTrendDiscovery


class _FakeRepo:
    """``SnapshotExistenceProtocol`` 互換の fake (test-only)。"""

    def __init__(self, exists: bool) -> None:
        self._exists = exists
        self.calls: list[date] = []

    async def exists_for_window_end(self, window_end: date) -> bool:
        self.calls.append(window_end)
        return self._exists


# frozen + 任意日付受け入れ


class TestStructure:
    def test_is_frozen(self) -> None:
        ready = ReadyForTrendDiscovery(window_end=date(2026, 5, 3))
        with pytest.raises(ValidationError):
            ready.window_end = date(2026, 4, 30)  # type: ignore[misc]

    @pytest.mark.parametrize(
        "any_date",
        [
            date(2026, 4, 20),  # monday
            date(2026, 4, 21),  # tuesday
            date(2026, 4, 25),  # saturday
            date(2026, 4, 26),  # sunday
            date(2026, 5, 3),  # sunday
        ],
    )
    def test_accepts_any_weekday(self, any_date: date) -> None:
        """rolling 7d window では月曜縛りはない (任意の JST 日付を受け入れる)。"""
        ready = ReadyForTrendDiscovery(window_end=any_date)
        assert ready.window_end == any_date


class TestTryAdvanceFrom:
    @pytest.mark.asyncio
    async def test_returns_ready_when_snapshot_absent(self) -> None:
        """既存 snapshot なし → Ready を返す。"""
        repo = _FakeRepo(exists=False)
        ready = await ReadyForTrendDiscovery.try_advance_from(
            window_end=date(2026, 5, 3), snapshot_repo=repo
        )
        assert ready is not None
        assert ready.window_end == date(2026, 5, 3)
        assert repo.calls == [date(2026, 5, 3)]

    @pytest.mark.asyncio
    async def test_returns_none_when_existing(self) -> None:
        """既存 snapshot あり → None。"""
        repo = _FakeRepo(exists=True)
        ready = await ReadyForTrendDiscovery.try_advance_from(
            window_end=date(2026, 5, 3), snapshot_repo=repo
        )
        assert ready is None
