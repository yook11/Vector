"""``ReadyForDigest`` の precondition / 不変条件テスト (Phase 4 spec §6.4)。

検証する観点:
- ``model_validator`` で week_start の月曜以外を構造的に拒否する (JST 月曜のみ通過)
- ``try_advance_from`` の precondition: exists_for_week + force の組み合わせ
- frozen / force default
"""

from __future__ import annotations

from datetime import date

import pytest
from pydantic import ValidationError

from app.digest.domain.ready import ReadyForDigest


class _FakeRepo:
    """``SnapshotExistenceProtocol`` 互換の fake (test-only)。"""

    def __init__(self, exists: bool) -> None:
        self._exists = exists
        self.calls: list[date] = []

    async def exists_for_week(self, week_start: date) -> bool:
        self.calls.append(week_start)
        return self._exists


# ---------------------------------------------------------------------------
# week_start の月曜検証 (model_validator)
# ---------------------------------------------------------------------------


class TestMondayValidator:
    def test_accepts_monday(self) -> None:
        ready = ReadyForDigest(week_start=date(2026, 4, 20))
        assert ready.week_start == date(2026, 4, 20)
        assert ready.week_start.weekday() == 0

    @pytest.mark.parametrize(
        "non_monday",
        [
            date(2026, 4, 21),  # tuesday
            date(2026, 4, 22),  # wednesday
            date(2026, 4, 23),  # thursday
            date(2026, 4, 24),  # friday
            date(2026, 4, 25),  # saturday
            date(2026, 4, 26),  # sunday
        ],
    )
    def test_rejects_non_monday(self, non_monday: date) -> None:
        with pytest.raises(ValidationError) as excinfo:
            ReadyForDigest(week_start=non_monday)
        assert "Monday" in str(excinfo.value)


# ---------------------------------------------------------------------------
# force default + frozen
# ---------------------------------------------------------------------------


class TestStructure:
    def test_force_defaults_to_false(self) -> None:
        ready = ReadyForDigest(week_start=date(2026, 4, 20))
        assert ready.force is False

    def test_force_can_be_set_true(self) -> None:
        ready = ReadyForDigest(week_start=date(2026, 4, 20), force=True)
        assert ready.force is True

    def test_is_frozen(self) -> None:
        ready = ReadyForDigest(week_start=date(2026, 4, 20))
        with pytest.raises(ValidationError):
            ready.week_start = date(2026, 4, 13)  # type: ignore[misc]


# ---------------------------------------------------------------------------
# try_advance_from
# ---------------------------------------------------------------------------


class TestTryAdvanceFrom:
    @pytest.mark.asyncio
    async def test_returns_ready_when_snapshot_absent(self) -> None:
        """既存 snapshot なし → Ready を返す (force 値に関わらず)。"""
        repo = _FakeRepo(exists=False)
        ready = await ReadyForDigest.try_advance_from(
            week_start=date(2026, 4, 20), force=False, snapshot_repo=repo
        )
        assert ready is not None
        assert ready.week_start == date(2026, 4, 20)
        assert ready.force is False
        assert repo.calls == [date(2026, 4, 20)]

    @pytest.mark.asyncio
    async def test_returns_none_when_existing_and_not_force(self) -> None:
        """既存 snapshot あり + force=False → None。"""
        repo = _FakeRepo(exists=True)
        ready = await ReadyForDigest.try_advance_from(
            week_start=date(2026, 4, 20), force=False, snapshot_repo=repo
        )
        assert ready is None

    @pytest.mark.asyncio
    async def test_returns_ready_when_existing_and_force(self) -> None:
        """既存 snapshot あり + force=True → Ready を返す (上書き経路)。

        force=True のとき exists 判定はスキップされ Repository は呼ばれない
        (短絡評価)。
        """
        repo = _FakeRepo(exists=True)
        ready = await ReadyForDigest.try_advance_from(
            week_start=date(2026, 4, 20), force=True, snapshot_repo=repo
        )
        assert ready is not None
        assert ready.force is True
        assert repo.calls == []  # 短絡評価で exists は呼ばない

    @pytest.mark.asyncio
    async def test_rejects_non_monday_via_validator(self) -> None:
        """try_advance_from でも構築段階で week_start が月曜以外を拒否する。"""
        repo = _FakeRepo(exists=False)
        with pytest.raises(ValidationError):
            await ReadyForDigest.try_advance_from(
                week_start=date(2026, 4, 21),  # tuesday
                force=False,
                snapshot_repo=repo,
            )
